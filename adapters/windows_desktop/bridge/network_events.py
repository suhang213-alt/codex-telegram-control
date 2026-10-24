"""Local Windows IP change notifications; no network reads, requests or polling.

Install before the initial cloud recovery attempt. Each callback only sets the
caller's threading.Event; recovery and its bounded retries run outside the OS
callback. The event is a hint to retry, never proof of Internet availability.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import threading

_KINDS = ("interface", "address")
_CHANGE_TYPES = frozenset({0, 1, 2})  # Parameter, add, delete; initial is 3.
_LIVE_SUBSCRIPTIONS = set()  # Strong references until every callback is cancelled.


class NetworkEventError(RuntimeError):
    """Only fixed diagnostic codes; no addresses, interface names or API text."""


@dataclass
class _Registration:
    handle: int
    callback: object


class _WindowsNetworkApi:
    def __init__(self):
        if os.name != "nt":
            raise NetworkEventError("network_events_windows_required")
        self._library = ctypes.WinDLL("iphlpapi")
        # Row is deliberately opaque: it remains Windows-owned and is never
        # read, retained or freed. Both notifications have the same ABI.
        self._callback_type = ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
        self._functions = {
            "interface": self._library.NotifyIpInterfaceChange,
            "address": self._library.NotifyUnicastIpAddressChange,
        }
        for function in self._functions.values():
            function.argtypes = [
                ctypes.c_ushort, self._callback_type, ctypes.c_void_p,
                ctypes.c_ubyte, ctypes.POINTER(wintypes.HANDLE)]
            function.restype = wintypes.ULONG
        self._library.CancelMibChangeNotify2.argtypes = [wintypes.HANDLE]
        self._library.CancelMibChangeNotify2.restype = wintypes.ULONG

    def register(self, kind, callback):
        native_callback = self._callback_type(callback)
        handle = wintypes.HANDLE()
        result = self._functions[kind](
            0, native_callback, None, 0, ctypes.byref(handle))
        # AF_UNSPEC = IPv4 + IPv6. InitialNotification = FALSE: initial
        # recovery is explicit and must start after both registrations.
        if result != 0 or not handle.value:
            raise NetworkEventError("network_events_registration_failed")
        return _Registration(int(handle.value), native_callback)

    def cancel(self, registration):
        if self._library.CancelMibChangeNotify2(registration.handle) != 0:
            raise NetworkEventError("network_events_cancel_failed")


class NetworkChangeEvents:
    """One explicit start/close subscription; no threads, windows or timers.

    The caller must retain the object and close it on shutdown. A strong
    module-level reference additionally protects native callback lifetimes
    against accidental early collection. A failed cancellation retains its
    references and is surfaced to the caller; close may then be retried.
    """
    def __init__(self, wake_event, *, backend=None):
        if not isinstance(wake_event, threading.Event):
            raise TypeError("network_events_requires_threading_event")
        self._wake = wake_event
        self._backend = backend if backend is not None else _WindowsNetworkApi()
        self._operations = threading.Lock()
        self._closed = threading.Event()
        self._state = "new"
        self._registrations = []
        self.last_error = None

    def _notify(self, context, row, notification_type):
        # No lifecycle lock: cancellation can wait for this callback to finish.
        # Never cancel subscriptions, invoke recovery or perform I/O here.
        if self._closed.is_set() or notification_type not in _CHANGE_TYPES:
            return
        try:
            self._wake.set()
        except Exception:
            # Exceptions must never cross the C callback boundary.
            self.last_error = "network_events_wake_failed"

    def _cancel_registered(self):
        failed = []
        for registration in reversed(self._registrations):
            try:
                self._backend.cancel(registration)
            except Exception:
                failed.append(registration)
        self._registrations = list(reversed(failed))
        if failed:
            self._state = "cancel_failed"
            self.last_error = "network_events_cancel_failed"
            # Keep this object, API DLL and every unconfirmed callback alive.
            raise NetworkEventError("network_events_cancel_failed")
        self._state = "closed"
        _LIVE_SUBSCRIPTIONS.discard(self)

    def start(self):
        with self._operations:
            if self._state != "new":
                raise NetworkEventError("network_events_not_restartable")
            self._state = "starting"
            _LIVE_SUBSCRIPTIONS.add(self)
            try:
                for kind in _KINDS:
                    self._registrations.append(self._backend.register(kind, self._notify))
            except Exception:
                self._closed.set()
                self.last_error = "network_events_registration_failed"
                try:
                    self._cancel_registered()
                except NetworkEventError:
                    raise
                raise NetworkEventError("network_events_registration_failed") from None
            self._state = "started"
        return self

    def close(self):
        # This API accepts an Event rather than an arbitrary callback, so the
        # native callback cannot call close or wait for this operation lock.
        with self._operations:
            self._closed.set()
            if self._state == "closed":
                return
            self._cancel_registered()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


def probe():
    """Subscribe/cancel only; never changes interface state or sends traffic."""
    event = threading.Event()
    subscription = NetworkChangeEvents(event)
    try:
        subscription.start()
    finally:
        subscription.close()
    return {"ok": True, "subscriptions": 2, "cancelled": True,
            "networkConfigurationChanged": False}


if __name__ == "__main__":
    import json
    import sys
    if sys.argv[1:] != ["probe"]:
        raise SystemExit(2)
    try:
        result = probe()
    except Exception:
        result = {"ok": False, "status": "network_events_probe_failed"}
    if sys.stdout is not None:
        print(json.dumps(result, separators=(",", ":")))
    raise SystemExit(0 if result["ok"] else 1)

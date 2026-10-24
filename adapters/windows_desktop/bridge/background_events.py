"""Wake the background bridge with one Windows auto-reset named event.

The event contains no command or text. Durable pending remains authoritative:
the service must scan it once after creating the event, and drain it after
each wake. Auto-reset events coalesce multiple signals; they are not a queue.

Local namespace intentionally binds producer and consumer to the same Windows
login session. An interactive logon task fits this contract; a Session 0
service does not. No polling loop or chat fallback is present in this module.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import getpass
import hashlib
import math
import os
from pathlib import Path
import threading


_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_EVENT_MODIFY_STATE = 0x0002
_ERROR_FILE_NOT_FOUND = 2


class BackgroundEventError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _username():
    if os.name != "nt":
        return getpass.getuser()
    api = ctypes.WinDLL("advapi32", use_last_error=True).GetUserNameW
    api.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.restype = wintypes.BOOL
    name = ctypes.create_unicode_buffer(257)
    length = wintypes.DWORD(len(name))
    if not api(name, ctypes.byref(length)):
        raise BackgroundEventError("event_username_unavailable")
    return name.value


def event_name(bridge_root=None, *, username=None):
    """Stable, opaque name from the actual user and canonical bridge root."""
    user = _username() if username is None else username
    if type(user) is not str or not user or "\0" in user or len(user) > 256:
        raise ValueError("event_username_invalid")
    root = Path(__file__).resolve().parent if bridge_root is None else Path(bridge_root).resolve()
    canonical = str(root).replace("\\", "/").rstrip("/").casefold()
    digest = hashlib.sha256(
        (user.casefold() + "\0" + canonical).encode("utf-8"),
    ).hexdigest()
    return "Local\\CodexTelegramDesktopBackground-" + digest


class _WindowsKernel:
    def __init__(self):
        if os.name != "nt":
            raise BackgroundEventError("windows_event_required")
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        self.CreateEventW = library.CreateEventW
        self.CreateEventW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        self.CreateEventW.restype = wintypes.HANDLE
        self.OpenEventW = library.OpenEventW
        self.OpenEventW.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        self.OpenEventW.restype = wintypes.HANDLE
        self.SetEvent = library.SetEvent
        self.SetEvent.argtypes = [wintypes.HANDLE]
        self.SetEvent.restype = wintypes.BOOL
        self.WaitForSingleObject = library.WaitForSingleObject
        self.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.WaitForSingleObject.restype = wintypes.DWORD
        self.CloseHandle = library.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.CloseHandle.restype = wintypes.BOOL

    @staticmethod
    def get_last_error():
        return ctypes.get_last_error()


def _milliseconds(timeout):
    if timeout is None:
        return _INFINITE
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("event_timeout_invalid")
    milliseconds = math.ceil(timeout * 1000)
    if milliseconds >= _INFINITE:
        raise ValueError("event_timeout_invalid")
    return milliseconds


class BackgroundEvent:
    def __init__(self, bridge_root=None, *, username=None, kernel=None):
        self.name = event_name(bridge_root, username=username)
        self._kernel = _WindowsKernel() if kernel is None else kernel
        self._lock = threading.RLock()
        self._waiters = 0
        # bManualReset=False, bInitialState=False; existing state is preserved
        # by CreateEventW when another same-session process already owns it.
        self._handle = self._kernel.CreateEventW(None, False, False, self.name)
        if not self._handle:
            raise BackgroundEventError("event_create_failed")

    def _check_open(self):
        if self._handle is None:
            raise BackgroundEventError("event_closed")

    def set(self):
        with self._lock:
            self._check_open()
            if not self._kernel.SetEvent(self._handle):
                raise BackgroundEventError("event_set_failed")

    def wait(self, timeout=None):
        """Wait once; timeout is seconds, None means a genuine infinite wait."""
        milliseconds = _milliseconds(timeout)
        with self._lock:
            self._check_open()
            handle = self._handle
            self._waiters += 1
        try:
            outcome = self._kernel.WaitForSingleObject(handle, milliseconds)
            if outcome == _WAIT_OBJECT_0:
                return True
            if outcome == _WAIT_TIMEOUT:
                return False
            raise BackgroundEventError("event_wait_failed")
        finally:
            with self._lock:
                self._waiters -= 1

    def close(self):
        """Close after stopping/joining the consumer, not during native wait."""
        with self._lock:
            if self._handle is None:
                return
            # Win32 defines closing a handle during its wait as undefined.
            # Signal + join the service thread before releasing this resource.
            if self._waiters:
                raise BackgroundEventError("event_wait_in_progress")
            if not self._kernel.CloseHandle(self._handle):
                raise BackgroundEventError("event_close_failed")
            self._handle = None

    def __enter__(self):
        with self._lock:
            self._check_open()
        return self

    def __exit__(self, *_exc):
        self.close()


def notify(bridge_root=None, *, username=None, kernel=None):
    """Signal the existing consumer; return False if it has not started.

    Commit durable pending before calling. A missing consumer is not an error
    receipt and must never cause an uncertain command to be re-executed.
    """
    api = _WindowsKernel() if kernel is None else kernel
    name = event_name(bridge_root, username=username)
    handle = api.OpenEventW(_EVENT_MODIFY_STATE, False, name)
    if not handle:
        if api.get_last_error() == _ERROR_FILE_NOT_FOUND:
            return False
        raise BackgroundEventError("event_open_failed")
    try:
        if not api.SetEvent(handle):
            raise BackgroundEventError("event_set_failed")
    except BaseException:
        api.CloseHandle(handle)
        raise
    if not api.CloseHandle(handle):
        raise BackgroundEventError("event_close_failed")
    return True

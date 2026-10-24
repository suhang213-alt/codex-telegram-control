"""Windows window lifecycle signals; no UI contents or UI actions.

An out-of-context WinEvent hook observes only window create/show/destroy. The
native callback enqueues metadata; a worker verifies the process belongs to the
installed Codex package and current Windows session before signaling the bridge.
Signals are hints to perform fresh native discovery, not proof MCP is ready.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import queue
import threading
from typing import Callable

from native_discovery import desktop_process_predicate


EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
_SUPPORTED_EVENTS = frozenset({
    EVENT_OBJECT_CREATE, EVENT_OBJECT_DESTROY, EVENT_OBJECT_SHOW,
})


class LifecycleError(RuntimeError):
    """Fixed sanitized diagnostic code."""


@dataclass(frozen=True)
class DesktopEvent:
    kind: str
    process_id: int


class WindowEventFilter:
    """Pure metadata filter, separately testable without Windows hooks."""

    def __init__(self, verifier: Callable[[int], bool]):
        self._verifier = verifier
        self._windows: dict[int, int] = {}

    def accept(self, event: int, hwnd: int, object_id: int, child_id: int,
               pid: int) -> DesktopEvent | None:
        if event not in _SUPPORTED_EVENTS or object_id != 0 or child_id != 0 or not hwnd:
            return None
        previous = self._windows.get(hwnd)
        if event == EVENT_OBJECT_DESTROY:
            self._windows.pop(hwnd, None)
            # A destroyed HWND may already have no discoverable PID.
            if previous is not None and (pid == 0 or pid == previous):
                return DesktopEvent("desktop_window_destroyed", previous)
            return None
        if not pid or not self._verifier(pid):
            self._windows.pop(hwnd, None)
            return None
        if len(self._windows) >= 512 and hwnd not in self._windows:
            # Never grow an unbounded cache. Repeated verified SHOW can still
            # signal discovery even if it cannot be tracked for destruction.
            return DesktopEvent("desktop_window_shown", pid)
        self._windows[hwnd] = pid
        return DesktopEvent(
            "desktop_window_created" if event == EVENT_OBJECT_CREATE else "desktop_window_shown",
            pid,
        )


class DesktopLifecycle:
    """Explicit start/close subscription; does not install itself or autostart."""

    def __init__(self, on_event: Callable[[DesktopEvent], None], *,
                 process_verifier: Callable[[int], bool] | None = None,
                 on_error: Callable[[str], None] | None = None):
        if os.name != "nt":
            raise LifecycleError("desktop_lifecycle_windows_required")
        self._on_event = on_event
        self._on_error = on_error
        self._filter = WindowEventFilter(
            process_verifier if process_verifier is not None else desktop_process_predicate()
        )
        self._events: queue.Queue = queue.Queue(maxsize=4096)
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._startup_error: str | None = None
        self._thread_id = 0
        self._pump: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._configure_api()

    def _configure_api(self):
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._callback_type = ctypes.WINFUNCTYPE(
            None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
        )
        self._user.SetWinEventHook.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
            self._callback_type, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ]
        self._user.SetWinEventHook.restype = wintypes.HANDLE
        self._user.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        self._user.UnhookWinEvent.restype = wintypes.BOOL
        self._user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        self._user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self._user.GetAncestor.restype = wintypes.HWND
        self._user.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
        ]
        self._user.GetMessageW.restype = ctypes.c_int
        self._user.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
            wintypes.UINT, wintypes.UINT,
        ]
        self._user.PeekMessageW.restype = wintypes.BOOL
        self._user.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self._user.PostThreadMessageW.restype = wintypes.BOOL
        self._kernel.GetCurrentThreadId.argtypes = []
        self._kernel.GetCurrentThreadId.restype = wintypes.DWORD

    def start(self):
        if self._pump is not None or self._closed.is_set():
            raise LifecycleError("desktop_lifecycle_not_restartable")
        self._worker = threading.Thread(target=self._dispatch, daemon=True)
        self._pump = threading.Thread(target=self._message_pump, daemon=True)
        self._worker.start()
        self._pump.start()
        if not self._ready.wait(5):
            self.close()
            raise LifecycleError("desktop_lifecycle_start_timeout")
        if self._startup_error:
            error = self._startup_error
            self.close()
            raise LifecycleError(error)
        return self

    def _native_event(self, hook, event, hwnd, object_id, child_id, event_thread, timestamp):
        # No window title, text, screenshot, or app data is accessed.
        if self._closed.is_set() or object_id != 0 or child_id != 0 or not hwnd:
            return
        # UI control creation is not a Desktop lifecycle signal. A destroyed
        # top-level window may no longer have ancestors; the worker only
        # accepts destroy for a previously verified tracked HWND.
        if event != EVENT_OBJECT_DESTROY and self._user.GetAncestor(hwnd, 2) != hwnd:
            return
        pid = wintypes.DWORD()
        self._user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            self._events.put_nowait((int(event), int(hwnd), int(object_id), int(child_id), pid.value))
        except queue.Full:
            # The callback never blocks Windows event dispatch. Report a
            # fixed failure once and stop; never quietly claim full coverage.
            self._startup_error = "desktop_lifecycle_event_overflow"
            self._closed.set()
            self._user.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)

    def _message_pump(self):
        hook = None
        try:
            self._thread_id = self._kernel.GetCurrentThreadId()
            message = wintypes.MSG()
            self._user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._callback = self._callback_type(self._native_event)
            hook = self._user.SetWinEventHook(
                EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW, None,
                self._callback, 0, 0, 0,  # WINEVENT_OUTOFCONTEXT
            )
            if not hook:
                self._startup_error = "desktop_lifecycle_hook_unavailable"
                return
            self._ready.set()
            while not self._closed.is_set():
                result = self._user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result < 0:
                    self._startup_error = "desktop_lifecycle_message_error"
                    break
        except Exception:
            self._startup_error = "desktop_lifecycle_failed"
        finally:
            if hook:
                self._user.UnhookWinEvent(hook)
            self._ready.set()
            self._closed.set()
            try:
                self._events.put_nowait(None)
            except queue.Full:
                pass
            if self._startup_error and self._on_error is not None:
                try:
                    self._on_error(self._startup_error)
                except Exception:
                    pass

    def _dispatch(self):
        while not self._closed.is_set():
            item = self._events.get()
            if item is None:
                break
            try:
                event = self._filter.accept(*item)
                if event is not None:
                    self._on_event(event)
            except Exception:
                if self._on_error is not None:
                    try:
                        self._on_error("desktop_lifecycle_callback_failed")
                    except Exception:
                        pass

    def close(self):
        self._closed.set()
        if self._thread_id:
            self._user.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        try:
            self._events.put_nowait(None)
        except queue.Full:
            pass
        for thread in (self._pump, self._worker):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

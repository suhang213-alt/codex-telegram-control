"""Read-only, shared Windows oplock events for an existing local file.

Only OPLOCK_LEVEL_CACHE_READ is requested. Microsoft documents its write break
as advisory: no acknowledgement is required and the writing operation proceeds
immediately. It is not a lock that delays or grants permission to the writer.

The next request is armed before invoking the callback. Callers must perform a
fresh read, retain partial trailing records, and retain a directory fallback for
replacement/deletion. No periodic polling, file writes, or exclusive oplocks.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


class _RequestInput(ctypes.Structure):
    _fields_ = [("version", wintypes.WORD), ("length", wintypes.WORD),
                ("level", wintypes.DWORD), ("flags", wintypes.DWORD)]


class _RequestOutput(ctypes.Structure):
    _fields_ = [("version", wintypes.WORD), ("length", wintypes.WORD),
                ("original", wintypes.DWORD), ("new", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("access", wintypes.DWORD),
                ("share", wintypes.WORD)]


class _WinReadDriver:
    def __init__(self, path):
        if os.name != "nt":
            raise OSError("read_events_require_windows")
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)]
        k.DeviceIoControl.restype = wintypes.BOOL
        k.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped),
            ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        k.GetOverlappedResult.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k.CancelIoEx.restype = wintypes.BOOL
        k.ResetEvent.argtypes = [wintypes.HANDLE]
        k.ResetEvent.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        self.k, self.handle, self.event = k, None, None
        self.request, self.pending = None, False
        try:
            # GENERIC_READ; share read/write/delete; existing file; overlapped I/O.
            self.handle = k.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x40000000, None)
            if self.handle == ctypes.c_void_p(-1).value:
                self.handle = None
                raise OSError("read_event_open_failed")
            self.event = k.CreateEventW(None, True, False, None)
            if not self.event:
                raise OSError("read_event_create_failed")
        except Exception:
            self.close()
            raise

    def arm(self):
        if self.pending:
            raise OSError("read_event_request_pending")
        ov = _Overlapped(); ov.hEvent = self.event
        inp = _RequestInput(1, ctypes.sizeof(_RequestInput), 1, 1)
        out = _RequestOutput(); count = wintypes.DWORD()
        self.request = (ov, inp, out, count)  # Keep async buffers alive until completion.
        if not self.k.ResetEvent(self.event):
            raise OSError("read_event_reset_failed")
        ctypes.set_last_error(0)
        result = self.k.DeviceIoControl(self.handle, 0x90240,
            ctypes.byref(inp), ctypes.sizeof(inp), ctypes.byref(out), ctypes.sizeof(out),
            ctypes.byref(count), ctypes.byref(ov))
        if result or ctypes.get_last_error() != 997:  # ERROR_IO_PENDING means granted.
            raise OSError("read_oplock_not_granted")
        self.pending = True

    def wait(self):
        if self.k.WaitForSingleObject(self.event, 0xFFFFFFFF) != 0:
            raise OSError("read_event_wait_failed")
        ov, _inp, out, count = self.request
        completed = self.k.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(count), False)
        self.pending = False
        if not completed:
            raise OSError("read_event_completion_failed")
        # A regular filesystem-operation break of our shared read request only.
        if ov.Internal != 0 or out.original != 1 or out.new != 0 or out.flags & 1:
            raise OSError("read_event_unexpected_break")

    def cancel(self):
        if self.handle:
            self.k.CancelIoEx(self.handle, None)

    def close(self):
        if self.handle:
            self.cancel()
            if self.pending and self.request is not None:
                ov, _inp, _out, count = self.request
                # Cancellation must complete before async buffers/handle are freed.
                self.k.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(count), True)
                self.pending = False
            self.k.CloseHandle(self.handle); self.handle = None
        if self.event:
            self.k.CloseHandle(self.event); self.event = None
        self.request = None


class WindowsReadEvents:
    """WindowsReadEvents(path, callback), with start(), close(), and safe .error.

start() returns only after an advisory read request is granted. Fatal errors set
.error and invoke callback once, so the owner can relinquish this event source.
close() is safe from callback and cancels an outstanding request without polling.
"""
    def __init__(self, path, callback, *, driver_factory=_WinReadDriver):
        self.path, self.callback, self.driver_factory = Path(path), callback, driver_factory
        self.ready, self.stopped = threading.Event(), threading.Event()
        self.error, self.driver = None, None
        self._driver_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="codex-result-read-events", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            self.close()
            raise OSError("read_event_registration_failed")

    def _arm(self):
        # Serializes cancellation with rearming. A close cannot cancel just before
        # a new request and leave an unobserved request waiting indefinitely.
        with self._driver_lock:
            if self.stopped.is_set():
                return False
            self.driver.arm()
            return True

    def _run(self):
        try:
            with self._driver_lock:
                self.driver = self.driver_factory(self.path)
            if not self._arm():
                return
            self.ready.set()
            while not self.stopped.is_set():
                self.driver.wait()
                if not self._arm():
                    break
                self.callback()
        except Exception:
            if not self.stopped.is_set():
                self.error = "read_events_failed"
                self.ready.set()
                try:
                    self.callback()
                except Exception:
                    pass
        finally:
            self.ready.set()
            with self._driver_lock:
                if self.driver is not None:
                    self.driver.close()
                    self.driver = None

    def close(self):
        with self._driver_lock:
            self.stopped.set()
            if self.driver is not None:
                self.driver.cancel()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(5)

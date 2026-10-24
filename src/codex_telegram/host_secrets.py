"""Owner-only host connector key checks, separate from Docker secret mounts."""
from __future__ import annotations
import os
from pathlib import Path
import re
import stat
from .config import SafeError


def _windows_acl(path):
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.EqualSid.argtypes = [pointer, pointer]
    security.EqualSid.restype = wintypes.BOOL
    security.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
    security.GetAce.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer

    class ACL(ctypes.Structure):
        _fields_ = [("revision", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte), ("size", ctypes.c_ushort),
                    ("count", ctypes.c_ushort), ("reserved2", ctypes.c_ushort)]

    token = wintypes.HANDLE()
    descriptor = pointer()
    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ValueError()
        required = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if not 0 < required.value <= 65536: raise ValueError()
        token_info = ctypes.create_string_buffer(required.value)
        if not security.GetTokenInformation(token, 1, token_info, required, ctypes.byref(required)):
            raise ValueError()
        user_sid = ctypes.cast(token_info, ctypes.POINTER(pointer))[0]
        owner, dacl = pointer(), pointer()
        if security.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None,
                                          ctypes.byref(descriptor)):
            raise ValueError()
        if not dacl.value or not security.EqualSid(owner, user_sid): raise ValueError()
        acl = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
        if not acl.count or acl.count > 128: raise ValueError()
        for index in range(acl.count):
            ace = pointer()
            if not security.GetAce(dacl, index, ctypes.byref(ace)): raise ValueError()
            # The setup creates only ordinary user ACCESS_ALLOWED_ACE entries.
            # Reject unknown ACE variants and every different principal.
            if ctypes.c_ubyte.from_address(ace.value).value != 0:
                raise ValueError()
            size = ctypes.c_ushort.from_address(ace.value + 2).value
            if size < 12 or not security.EqualSid(pointer(ace.value + 8), user_sid): raise ValueError()
    finally:
        if descriptor.value: kernel.LocalFree(descriptor)
        if token: kernel.CloseHandle(token)


def read_host_key(value):
    """Fail closed for direct env entrypoints as well as the setup wrapper."""
    path = Path(value).expanduser().absolute()
    descriptor = None
    try:
        for candidate in (path, *path.parents):
            if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)(): raise ValueError()
        for candidate in (path, path.parent):
            info = candidate.stat()
            if os.name == "nt":
                _windows_acl(candidate)
            elif info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError()
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024: raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino): raise ValueError()
        raw = os.read(descriptor, 1025).decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", raw): raise ValueError()
        return bytes.fromhex(raw)
    except (OSError, ValueError):
        raise SafeError("Host connector key must be a private owner-only file in a private directory, without redirected paths.") from None
    finally:
        if descriptor is not None: os.close(descriptor)

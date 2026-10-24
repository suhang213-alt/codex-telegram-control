"""Recognize Windows DOS aliases while rejecting actual path redirection."""
from __future__ import annotations
import ctypes
import os
from pathlib import Path


def _long_name(path):
    """Expand 8.3 components without following symlinks or junction targets."""
    if os.name != "nt":
        return path
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    count = function(str(path), buffer, len(buffer))
    if not count or count >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value)


def resolve_without_redirects(value):
    """Return an existing canonical local path, allowing only spelling aliases."""
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("unsafe_path")
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("redirected_path")
    resolved = path.resolve(strict=True)
    # GetLongPathNameW expands DOS names, whereas resolve follows redirects.
    # Equality still rejects any other name-changing reparse-point behavior.
    if resolved != _long_name(path.absolute()):
        raise ValueError("redirected_path")
    return resolved

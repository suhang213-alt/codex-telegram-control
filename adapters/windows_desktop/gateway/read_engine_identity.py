"""Read only the two non-secret Windows sandbox variables of an existing engine."""
import ctypes
import hashlib
import json
import os
from pathlib import Path

ALLOWLIST = ("CODEX_WINDOWS_REGISTERED_CORE", "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY")


def read_engine_identity(pid, *, include_cli_override=False):
    if os.name != "nt":
        raise RuntimeError("Windows required")
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        def read(address, size):
            data = ctypes.create_string_buffer(size)
            count = ctypes.c_size_t()
            if not kernel.ReadProcessMemory(handle, address, data, size, ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            return data.raw[:count.value]
        name = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        # x64 PEB/RTL_USER_PROCESS_PARAMETERS offsets; reject 32-bit interpreter.
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("64-bit interpreter required")
        basic = ctypes.create_string_buffer(48)
        returned = wintypes.ULONG()
        result = ntdll.NtQueryInformationProcess(handle, 0, basic, len(basic), ctypes.byref(returned))
        if result:
            raise RuntimeError("NtQueryInformationProcess failed: %08x" % (result & 0xffffffff))
        peb = int.from_bytes(basic.raw[8:16], "little")
        params = int.from_bytes(read(peb + 0x20, 8), "little")
        environment = int.from_bytes(read(params + 0x80, 8), "little")
        raw = bytearray()
        # Read bounded pages until the terminating UTF-16 double NUL. Only allowlisted
        # names are retained or returned; no environment dump is written or printed.
        for offset in range(0, 2 * 1024 * 1024, 4096):
            raw.extend(read(environment + offset, 4096))
            end = next((i for i in range(0, len(raw) - 3, 2) if raw[i:i + 4] == b"\0\0\0\0"), -1)
            if end >= 0:
                raw = raw[:end + 2]
                break
        else:
            raise RuntimeError("Environment exceeds read limit")
        names = ALLOWLIST + (("CODEX_CLI_PATH",) if include_cli_override else ())
        values = {key: None for key in names}
        for item in raw.decode("utf-16-le").split("\0"):
            key, separator, value = item.partition("=")
            if separator and key.upper() in values:
                values[key.upper()] = value
        del raw
        path = str(Path(name.value).resolve(strict=True))
        with open(path, "rb") as executable:
            digest = hashlib.file_digest(executable, "sha256").hexdigest()
        return {"pid": pid, "parent_pid": int.from_bytes(basic.raw[40:48], "little"),
                "executable": path, "sha256": digest, "sandbox_environment": values}
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    args = parser.parse_args()
    print(json.dumps(read_engine_identity(args.pid), ensure_ascii=False, indent=2))

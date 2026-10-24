"""Discover the installed Desktop MCP endpoint without process-env extraction.

This is a version-dependent Windows adapter around the shipped named-pipe
transport, not a public discovery API. It never starts Desktop or model turns.
The caller must still use the shipped MCP server and enforce owner authorization.
"""
from __future__ import annotations
import bridge_config as config

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import os
from pathlib import Path, PureWindowsPath
import re
from typing import Protocol


SERVICE_THREAD_ID = config.service_thread_id()
PACKAGE_FAMILY = "OpenAI.Codex_2p2nqsd0c76g0"
PIPE_PREFIX = "\\\\.\\pipe\\codex-app-tools-"
PIPE_PATTERN = re.compile(
    re.escape(PIPE_PREFIX)
    + r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
MAX_CANDIDATES = 64
PACKAGE_FULL_NAME = re.compile(
    r"OpenAI\.Codex_\d+(?:\.\d+){3}_(?:x64|arm64|x86|neutral)_[^\\/_]*_2p2nqsd0c76g0"
)
APPMODEL_ERROR_NO_PACKAGE = 15700


class DiscoveryError(RuntimeError):
    """A fixed diagnostic code only; never include endpoint or account data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PipeIdentity:
    image_path: str = field(repr=False)
    package_family: str
    package_full_name: str = field(repr=False)
    package_path: str = field(repr=False)
    server_session_id: int
    current_session_id: int


class DiscoveryBackend(Protocol):
    def enumerate_pipe_paths(self) -> list[str]: ...
    def inspect_pipe(self, pipe_path: str) -> PipeIdentity | None: ...
    def is_file(self, path: str) -> bool: ...


def desktop_process_predicate():
    """Return a reusable package/session verifier for OS lifecycle events."""
    adapter = _WindowsBackend()

    def verify(pid: int) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        try:
            identity = adapter.inspect_process(pid)
            return identity is not None and _eligible_node(identity) is not None
        except Exception:
            return False

    return verify


def _canonical_windows_path(value: str) -> str:
    # No realpath or remote filesystem traversal: these paths come from the
    # process/package APIs. Reject relative/device/UNC paths before comparison.
    parsed = PureWindowsPath(value)
    if not parsed.is_absolute() or not re.fullmatch(r"[a-zA-Z]:", parsed.drive):
        return ""
    if ".." in parsed.parts:
        return ""
    return str(parsed).casefold()


def _eligible_node(identity: PipeIdentity) -> str | None:
    if identity.package_family != PACKAGE_FAMILY:
        return None
    if identity.server_session_id != identity.current_session_id:
        return None
    if PACKAGE_FULL_NAME.fullmatch(identity.package_full_name) is None:
        return None
    package = PureWindowsPath(identity.package_path)
    if package.name.casefold() != identity.package_full_name.casefold():
        return None
    if not _canonical_windows_path(identity.package_path):
        return None
    expected_image = str(package / "app" / "ChatGPT.exe")
    if _canonical_windows_path(identity.image_path) != _canonical_windows_path(expected_image):
        return None
    return str(package / "app" / "resources" / "cua_node" / "bin" / "node.exe")


def discover_environment(
    *, caller_thread_id: str = SERVICE_THREAD_ID,
    backend: DiscoveryBackend | None = None,
) -> dict[str, str]:
    """Return fresh native MCP env overrides for the existing service context.

    No fallback chooses the first process, uses PATH node, or trusts a saved
    pipe. A missing or ambiguous Desktop leaves queued commands untouched.
    """
    if caller_thread_id != SERVICE_THREAD_ID:
        raise DiscoveryError("native_discovery_caller_context_mismatch")
    try:
        adapter = backend if backend is not None else _WindowsBackend()
        paths = adapter.enumerate_pipe_paths()
        if len(paths) > MAX_CANDIDATES:
            raise DiscoveryError("native_discovery_candidate_limit")
        eligible: dict[str, tuple[str, str]] = {}
        for pipe_path in paths:
            if not isinstance(pipe_path, str) or PIPE_PATTERN.fullmatch(pipe_path) is None:
                continue
            identity = adapter.inspect_pipe(pipe_path)
            if identity is None:
                continue
            node = _eligible_node(identity)
            if node is not None and adapter.is_file(node):
                eligible[pipe_path.casefold()] = (pipe_path, node)
        if not eligible:
            raise DiscoveryError("native_desktop_unavailable")
        if len(eligible) != 1:
            raise DiscoveryError("native_desktop_ambiguous")
        pipe_path, node = next(iter(eligible.values()))
        return {
            "CODEX_APP_TOOLS_PIPE_PATH": pipe_path,
            "CODEX_MCP_NODE_PATH": node,
            "CODEX_THREAD_ID": SERVICE_THREAD_ID,
        }
    except DiscoveryError:
        raise
    except Exception:
        # OS exceptions may contain named pipes or local paths.
        raise DiscoveryError("native_discovery_failed") from None


class _WindowsBackend:
    """Only enumerate pipe names and inspect their Windows server identity."""

    def __init__(self):
        if os.name != "nt":
            raise DiscoveryError("native_discovery_windows_required")
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._invalid_handle = ctypes.c_void_p(-1).value
        self._configure_api()

    def _configure_api(self):
        kernel = self._kernel
        pointer = ctypes.c_void_p
        dword_ptr = ctypes.POINTER(wintypes.DWORD)

        class FindData(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("created", wintypes.FILETIME),
                ("accessed", wintypes.FILETIME),
                ("written", wintypes.FILETIME),
                ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                ("reserved0", wintypes.DWORD), ("reserved1", wintypes.DWORD),
                ("filename", wintypes.WCHAR * 260),
                ("alternate", wintypes.WCHAR * 14),
            ]

        self._find_data_type = FindData
        kernel.FindFirstFileW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(FindData)]
        kernel.FindFirstFileW.restype = wintypes.HANDLE
        kernel.FindNextFileW.argtypes = [wintypes.HANDLE, ctypes.POINTER(FindData)]
        kernel.FindNextFileW.restype = wintypes.BOOL
        kernel.FindClose.argtypes = [wintypes.HANDLE]
        kernel.FindClose.restype = wintypes.BOOL
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, pointer,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, dword_ptr]
        kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetCurrentProcessId.argtypes = []
        kernel.GetCurrentProcessId.restype = wintypes.DWORD
        kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, dword_ptr]
        kernel.ProcessIdToSessionId.restype = wintypes.BOOL
        kernel.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, dword_ptr,
        ]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        for name in ("GetPackageFamilyName", "GetPackageFullName"):
            function = getattr(kernel, name)
            function.argtypes = [wintypes.HANDLE, dword_ptr, wintypes.LPWSTR]
            function.restype = wintypes.LONG
        kernel.GetPackagePathByFullName.argtypes = [
            wintypes.LPCWSTR, dword_ptr, wintypes.LPWSTR,
        ]
        kernel.GetPackagePathByFullName.restype = wintypes.LONG
        kernel.PackageFamilyNameFromFullName.argtypes = [
            wintypes.LPCWSTR, dword_ptr, wintypes.LPWSTR,
        ]
        kernel.PackageFamilyNameFromFullName.restype = wintypes.LONG

    def enumerate_pipe_paths(self) -> list[str]:
        data = self._find_data_type()
        handle = self._kernel.FindFirstFileW(PIPE_PREFIX + "*", ctypes.byref(data))
        if handle == self._invalid_handle:
            if ctypes.get_last_error() in (2, 3):
                return []
            raise DiscoveryError("native_discovery_enumeration_failed")
        paths = []
        try:
            while True:
                paths.append("\\\\.\\pipe\\" + data.filename)
                if len(paths) > MAX_CANDIDATES:
                    raise DiscoveryError("native_discovery_candidate_limit")
                if not self._kernel.FindNextFileW(handle, ctypes.byref(data)):
                    if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                        raise DiscoveryError("native_discovery_enumeration_failed")
                    return paths
        finally:
            self._kernel.FindClose(handle)

    def _package_text(self, function, subject) -> str | None:
        return self._package_text_result(function, subject)[1]

    def _package_text_result(self, function, subject) -> tuple[int, str | None]:
        size = wintypes.DWORD()
        error = function(subject, ctypes.byref(size), None)
        if error != 122 or not 1 < size.value <= 32768:
            return error, None
        result = ctypes.create_unicode_buffer(size.value)
        error = function(subject, ctypes.byref(size), result)
        if error != 0:
            return error, None
        return 0, result.value

    def _installed_package_for_image(self, image_path: str) -> tuple[str, str, str] | None:
        """Verify a directly launched installed image using OS package mapping.

        Direct execution of the official packaged exe can lack *process*
        package identity. A filename or directory alone is never evidence:
        the OS must confirm its publisher family and exact installation path.
        """
        if not _canonical_windows_path(image_path):
            return None
        image = PureWindowsPath(image_path)
        package = image.parent.parent
        full_name = package.name
        if (image.name.casefold() != "chatgpt.exe" or image.parent.name.casefold() != "app"
                or package.parent.name.casefold() != "windowsapps"
                or PACKAGE_FULL_NAME.fullmatch(full_name) is None):
            return None
        family = self._package_text(self._kernel.PackageFamilyNameFromFullName, full_name)
        if family != PACKAGE_FAMILY:
            return None
        installed_path = self._package_text(self._kernel.GetPackagePathByFullName, full_name)
        if (not installed_path or
                _canonical_windows_path(installed_path) != _canonical_windows_path(str(package))):
            return None
        return family, full_name, installed_path

    def inspect_pipe(self, pipe_path: str) -> PipeIdentity | None:
        kernel = self._kernel
        # READ_CONTROL only: no application payload is read/written.
        pipe = kernel.CreateFileW(pipe_path, 0x20000, 0, None, 3, 0, None)
        if pipe == self._invalid_handle:
            return None
        try:
            pid = wintypes.DWORD()
            if not kernel.GetNamedPipeServerProcessId(pipe, ctypes.byref(pid)):
                return None
            return self.inspect_process(pid.value)
        finally:
            kernel.CloseHandle(pipe)

    def inspect_process(self, pid: int) -> PipeIdentity | None:
        kernel = self._kernel
        process = None
        try:
            server_session = wintypes.DWORD()
            current_session = wintypes.DWORD()
            if not kernel.ProcessIdToSessionId(pid, ctypes.byref(server_session)):
                return None
            if not kernel.ProcessIdToSessionId(
                kernel.GetCurrentProcessId(), ctypes.byref(current_session)
            ):
                return None
            if server_session.value != current_session.value:
                return None
            process = kernel.OpenProcess(0x1000, False, pid)
            if not process:
                return None
            image = ctypes.create_unicode_buffer(32768)
            image_size = wintypes.DWORD(len(image))
            if not kernel.QueryFullProcessImageNameW(
                process, 0, image, ctypes.byref(image_size)
            ):
                return None
            family_error, family = self._package_text_result(kernel.GetPackageFamilyName, process)
            full_error, full_name = self._package_text_result(kernel.GetPackageFullName, process)
            if family_error == full_error == APPMODEL_ERROR_NO_PACKAGE:
                installed = self._installed_package_for_image(image.value)
                if installed is None:
                    return None
                family, full_name, package_path = installed
            else:
                if family != PACKAGE_FAMILY or not full_name:
                    return None
                package_path = self._package_text(kernel.GetPackagePathByFullName, full_name)
                if not package_path:
                    return None
            return PipeIdentity(
                image.value, family, full_name, package_path,
                server_session.value, current_session.value,
            )
        finally:
            if process:
                kernel.CloseHandle(process)

    def is_file(self, path: str) -> bool:
        return Path(path).is_file()

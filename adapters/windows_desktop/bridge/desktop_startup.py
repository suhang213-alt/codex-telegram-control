"""One authorised packaged-app activation per interactive Windows logon.

No StartupTask registry changes, scheduled-task edits, timer, service-chat
messages or model turns. Windows chooses the registered package activation.
Only "probe" is read-only; "ensure" is for the authorised AtLogon bootstrap.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

PACKAGE_FAMILY = "OpenAI.Codex_2p2nqsd0c76g0"
FULL_NAME = re.compile(
    r"OpenAI\.Codex_\d+(?:\.\d+){3}_(?:x64|arm64|x86|neutral)_[^\\/_]*_2p2nqsd0c76g0"
)
AO_NOERRORUI = 2


class StartupError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PackageTarget:
    full_name: str
    install_path: str
    app_id: str

    @property
    def aumid(self):
        return PACKAGE_FAMILY + "!" + self.app_id


def target_from_manifest(full_name, install_path, xml_text):
    """Accept only the registered Codex package and its declared desktop app."""
    if not isinstance(full_name, str) or not FULL_NAME.fullmatch(full_name):
        raise StartupError("startup_package_identity_invalid")
    root = PureWindowsPath(install_path)
    if (not root.is_absolute() or not re.fullmatch(r"[A-Za-z]:", root.drive)
            or ".." in root.parts or root.name.casefold() != full_name.casefold()):
        raise StartupError("startup_package_path_invalid")
    if not isinstance(xml_text, str) or len(xml_text) > 1_048_576:
        raise StartupError("startup_manifest_invalid")
    try:
        manifest = ET.fromstring(xml_text)
    except ET.ParseError:
        raise StartupError("startup_manifest_invalid") from None
    matches = []
    for item in manifest.iter():
        if item.tag.rsplit("}", 1)[-1] != "Application":
            continue
        app_id = item.get("Id")
        executable = str(item.get("Executable", "")).replace("\\", "/").casefold()
        if executable != "app/chatgpt.exe":
            continue
        if not isinstance(app_id, str) or not re.fullmatch(r"[A-Za-z0-9.]{1,64}", app_id):
            raise StartupError("startup_app_id_invalid")
        matches.append(PackageTarget(full_name, str(root), app_id))
    if len(matches) != 1:
        raise StartupError("startup_application_ambiguous")
    return matches[0]


def ensure_codex_started(backend):
    """One attempt, including across bridge restarts in the same logon.

    An activation receipt proves only that Windows accepted the launch. Native
    bridge readiness is established later by its existing lifecycle handler.
    """
    target = backend.target()
    logon_key = backend.logon_key()
    running = backend.is_running(target)
    if not backend.reserve_once(logon_key):
        return {"ok": True, "status": "already_checked", "activationAttempted": False}
    if running:
        return {"ok": True, "status": "already_running", "activationAttempted": False}
    try:
        pid = backend.activate(target.aumid, None, AO_NOERRORUI)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise StartupError("startup_activation_unconfirmed")
    except Exception:
        # The OS may have launched the app before returning an error. The
        # durable once marker remains; never automatically activate twice.
        return {"ok": False, "status": "activation_unconfirmed", "activationAttempted": True}
    return {"ok": True, "status": "launch_requested", "activationAttempted": True}


def console_python(executable):
    """Use this interpreter's verified neighbour, never PATH or a new runtime."""
    source = Path(executable).resolve(strict=True)
    if source.name.casefold() not in {"python.exe", "pythonw.exe"} or not source.is_file():
        raise StartupError("startup_python_runtime_unavailable")
    candidate = source.with_name("python.exe").resolve(strict=True)
    if candidate.parent != source.parent or candidate.name.casefold() != "python.exe" or not candidate.is_file():
        raise StartupError("startup_python_runtime_unavailable")
    return str(candidate)


def make_logon_key(session, low, high, logon_time):
    values = (session, low, high, logon_time)
    if (any(type(value) is not int for value in values)
            or not 0 < session <= 0xffffffff or not 0 <= low <= 0xffffffff
            or not -0x80000000 <= high <= 0x7fffffff
            or not 0 < logon_time <= 0x7fffffffffffffff):
        raise StartupError("startup_logon_identity_invalid")
    # LUID alone is unique only until reboot. LogonTime identifies its epoch.
    return f"{session:x}-{high & 0xffffffff:08x}{low:08x}-{logon_time:016x}"


def run_bootstrap_once(timeout=20):
    """Bounded hidden helper for the existing long-lived AtLogon bridge process.

    The caller should invoke once before starting ordinary bridge workers.
    This never changes the scheduled-task action or starts another bridge.
    """
    try:
        result = subprocess.run(
            [console_python(sys.executable), str(Path(__file__).resolve()), "ensure"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        value = json.loads(result.stdout.decode("utf-8"))
        if (not isinstance(value, dict) or set(value) != {"ok", "status", "activationAttempted"}
                or type(value["ok"]) is not bool
                or not isinstance(value["status"], str)
                or re.fullmatch(r"[a-z_]{1,80}", value["status"]) is None
                or type(value["activationAttempted"]) is not bool):
            raise ValueError()
        return value
    except Exception:
        return {"ok": False, "status": "bootstrap_unconfirmed", "activationAttempted": None}


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class WindowsStartupBackend:
    def __init__(self, marker_directory=None):
        if os.name != "nt":
            raise StartupError("startup_windows_required")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.GetCurrentProcessId.restype = wintypes.DWORD
        self.kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.GetPackagesByPackageFamily.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        self.kernel.GetPackagesByPackageFamily.restype = wintypes.LONG
        self.kernel.GetPackagePathByFullName.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        self.kernel.GetPackagePathByFullName.restype = wintypes.LONG
        local = os.environ.get("LOCALAPPDATA")
        if marker_directory is None and not local:
            raise StartupError("startup_local_profile_missing")
        self.marker_directory = Path(marker_directory) if marker_directory else Path(local) / "CodexTelegramBridge" / "startup"

    def _package_path(self, name):
        length = wintypes.UINT()
        if self.kernel.GetPackagePathByFullName(name, ctypes.byref(length), None) != 122 or not 1 < length.value <= 32768:
            raise StartupError("startup_package_path_unavailable")
        buffer = ctypes.create_unicode_buffer(length.value)
        if self.kernel.GetPackagePathByFullName(name, ctypes.byref(length), buffer):
            raise StartupError("startup_package_path_unavailable")
        return buffer.value

    def target(self):
        count, size = wintypes.UINT(), wintypes.UINT()
        error = self.kernel.GetPackagesByPackageFamily(PACKAGE_FAMILY, ctypes.byref(count), None, ctypes.byref(size), None)
        if error != 122 or not 1 <= count.value <= 16 or not 1 <= size.value <= 32768:
            raise StartupError("startup_registered_package_unavailable")
        names = (wintypes.LPWSTR * count.value)()
        buffer = ctypes.create_unicode_buffer(size.value)
        if self.kernel.GetPackagesByPackageFamily(PACKAGE_FAMILY, ctypes.byref(count), names, ctypes.byref(size), buffer):
            raise StartupError("startup_registered_package_unavailable")
        targets = []
        for name in names[:count.value]:
            if not FULL_NAME.fullmatch(name):
                continue
            path = self._package_path(name)
            manifest = Path(path) / "AppxManifest.xml"
            if manifest.stat().st_size > 1_048_576:
                raise StartupError("startup_manifest_invalid")
            target = target_from_manifest(name, path, manifest.read_text(encoding="utf-8-sig"))
            if not (Path(path) / "app" / "ChatGPT.exe").is_file():
                raise StartupError("startup_executable_missing")
            targets.append(target)
        if len(targets) != 1:
            raise StartupError("startup_registered_package_ambiguous")
        return targets[0]

    def logon_key(self):
        # AuthenticationId and LogonTime identify only this OS logon session.
        # Neither is a credential. LUID may be reused following a reboot.
        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]
        class Statistics(ctypes.Structure):
            _fields_ = [("TokenId", LUID), ("AuthenticationId", LUID),
                        ("ExpirationTime", ctypes.c_longlong), ("TokenType", wintypes.DWORD),
                        ("ImpersonationLevel", wintypes.DWORD), ("DynamicCharged", wintypes.DWORD),
                        ("DynamicAvailable", wintypes.DWORD), ("GroupCount", wintypes.DWORD),
                        ("PrivilegeCount", wintypes.DWORD), ("ModifiedId", LUID)]
        session = wintypes.DWORD()
        if not self.kernel.ProcessIdToSessionId(self.kernel.GetCurrentProcessId(), ctypes.byref(session)) or session.value == 0:
            raise StartupError("startup_interactive_session_required")
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise StartupError("startup_logon_identity_unavailable")
        try:
            value, size = Statistics(), wintypes.DWORD()
            if not advapi.GetTokenInformation(token, 10, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)):
                raise StartupError("startup_logon_identity_unavailable")
            authentication_id = value.AuthenticationId
            class UnicodeString(ctypes.Structure):
                _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                            ("Buffer", ctypes.c_void_p)]
            class LogonSessionPrefix(ctypes.Structure):
                _fields_ = [("Size", wintypes.ULONG), ("LogonId", LUID),
                            ("UserName", UnicodeString), ("LogonDomain", UnicodeString),
                            ("AuthenticationPackage", UnicodeString),
                            ("LogonType", wintypes.ULONG), ("Session", wintypes.ULONG),
                            ("Sid", ctypes.c_void_p), ("LogonTime", ctypes.c_longlong)]
            security = ctypes.WinDLL("secur32")
            security.LsaGetLogonSessionData.argtypes = [
                ctypes.POINTER(LUID), ctypes.POINTER(ctypes.POINTER(LogonSessionPrefix))]
            security.LsaGetLogonSessionData.restype = wintypes.LONG
            security.LsaFreeReturnBuffer.argtypes = [ctypes.c_void_p]
            security.LsaFreeReturnBuffer.restype = wintypes.LONG
            data = ctypes.POINTER(LogonSessionPrefix)()
            if security.LsaGetLogonSessionData(ctypes.byref(authentication_id), ctypes.byref(data)) != 0 or not data:
                raise StartupError("startup_logon_time_unavailable")
            try:
                entry = data.contents
                if (entry.Size < ctypes.sizeof(LogonSessionPrefix)
                        or entry.LogonId.LowPart != authentication_id.LowPart
                        or entry.LogonId.HighPart != authentication_id.HighPart):
                    raise StartupError("startup_logon_time_unavailable")
                # Names, SID and other returned pointers are never dereferenced.
                return make_logon_key(session.value, authentication_id.LowPart,
                                      authentication_id.HighPart, entry.LogonTime)
            finally:
                security.LsaFreeReturnBuffer(data)
        finally:
            self.kernel.CloseHandle(token)

    def reserve_once(self, logon_key):
        if not isinstance(logon_key, str) or not re.fullmatch(r"[0-9a-f]{1,8}-[0-9a-f]{16}-[0-9a-f]{16}", logon_key):
            raise StartupError("startup_logon_identity_invalid")
        self.marker_directory.mkdir(parents=True, exist_ok=True)
        path = self.marker_directory / (logon_key + ".checked")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        try:
            os.write(descriptor, b"checked-before-activation\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True

    def is_running(self, target):
        # Reuse the bridge's existing package, exact-image and same-session
        # verifier. Enumerate PIDs only; no process environment/command lines.
        from native_discovery import _WindowsBackend, _eligible_node
        adapter = _WindowsBackend()
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD)]
        processes, used = (wintypes.DWORD * 16384)(), wintypes.DWORD()
        if not psapi.EnumProcesses(processes, ctypes.sizeof(processes), ctypes.byref(used)):
            raise StartupError("startup_process_snapshot_unavailable")
        if used.value >= ctypes.sizeof(processes):
            raise StartupError("startup_process_snapshot_incomplete")
        for pid in processes[:used.value // ctypes.sizeof(wintypes.DWORD)]:
            identity = adapter.inspect_process(pid)
            if identity is not None and _eligible_node(identity) is not None:
                return True
        return False

    @contextmanager
    def _activation_manager(self):
        ole = ctypes.OleDLL("ole32")
        ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole.CoInitializeEx.restype = wintypes.LONG
        ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        ole.CoCreateInstance.restype = wintypes.LONG
        ole.CoUninitialize.argtypes = []
        initialized = ole.CoInitializeEx(None, 2)
        if initialized not in (0, 1):
            raise StartupError("startup_activation_context_unavailable")
        pointer = ctypes.c_void_p()
        try:
            clsid = GUID.parse("45BA127D-10A8-46EA-8AB7-56EA9078943C")
            iid = GUID.parse("2E941141-7F97-4756-BA1D-9DECDE894A3D")
            # Out-of-process COM lifetime is Microsoft's recommendation for
            # a short-lived application activation helper.
            if ole.CoCreateInstance(ctypes.byref(clsid), None, 4, ctypes.byref(iid), ctypes.byref(pointer)) < 0:
                raise StartupError("startup_activation_manager_unavailable")
            yield pointer
        finally:
            if pointer:
                table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(table[2])(pointer)
            ole.CoUninitialize()

    def activation_manager_available(self):
        # Capability check creates/releases COM interface; never ActivateApplication.
        with self._activation_manager():
            return True

    def activate(self, aumid, arguments, options):
        if not re.fullmatch(re.escape(PACKAGE_FAMILY) + r"![A-Za-z0-9.]{1,64}", aumid) or arguments is not None or options != AO_NOERRORUI:
            raise StartupError("startup_activation_arguments_invalid")
        with self._activation_manager() as pointer:
            table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            activate = ctypes.WINFUNCTYPE(wintypes.LONG, ctypes.c_void_p, wintypes.LPCWSTR,
                                          wintypes.LPCWSTR, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD))(table[3])
            pid = wintypes.DWORD()
            if activate(pointer, aumid, None, AO_NOERRORUI, ctypes.byref(pid)) < 0:
                raise StartupError("startup_activation_unconfirmed")
            return pid.value


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"probe", "ensure"}:
        return 2
    try:
        backend = WindowsStartupBackend()
        if sys.argv[1] == "probe":
            target = backend.target()
            backend.logon_key()
            value = {"ok": True, "packageVerified": True, "aumid": target.aumid,
                     "activationManagerAvailable": backend.activation_manager_available(),
                     "alreadyRunning": backend.is_running(target), "activationAttempted": False}
        else:
            value = ensure_codex_started(backend)
    except Exception as error:
        value = {"ok": False, "status": error.code if isinstance(error, StartupError) else "startup_unavailable",
                 "activationAttempted": False}
    if sys.stdout is not None:
        print(json.dumps(value, separators=(",", ":")))
    return 0 if value["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

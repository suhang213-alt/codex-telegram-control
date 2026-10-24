"""Verified, process-local launch of the installed macOS Desktop application.

No login, profile copying, credential reads, application modifications, global
environment changes, or permission/sandbox overrides are performed here.
The private Desktop launch contract requires a live compatibility check on Mac.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile

from .config import SafeError, atomic_json

# First-party identity: openai/codex, codex-rs/cli/src/desktop_app/mac.rs.
# The same requirement is present in the installed Desktop bootstrap.
BUNDLE_ID = "com.openai.codex"
TEAM_ID = "2DC432GLL2"
CLI_RESOURCE = Path("Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
MACHO_MAGICS = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
_SECRET_ENV = re.compile(r"(?:TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL)", re.I)


def desktop_home() -> Path:
    override = os.environ.get("CODEX_TELEGRAM_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library/Application Support/codex-telegram-desktop"


def require_macos() -> None:
    if sys.platform != "darwin":
        raise SafeError("Existing Desktop integration requires macOS. This command did not launch an engine.")


def _command(arguments: list[str], *, timeout: int = 30):
    try:
        return subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              errors="replace", check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise SafeError("A macOS identity check could not run. No application was launched.") from None


def file_hash(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside_file(root: Path, relative: Path) -> Path:
    expected = root / relative
    try:
        actual = expected.resolve(strict=True)
        if not actual.is_relative_to(root) or actual != expected or not actual.is_file():
            raise ValueError()
        mode = actual.stat().st_mode
        if mode & (stat.S_IWGRP | stat.S_IWOTH) or not mode & 0o111:
            raise ValueError()
        with actual.open("rb") as stream:
            if stream.read(4) not in MACHO_MAGICS:
                raise ValueError()
        return actual
    except (OSError, ValueError):
        raise SafeError("The signed Desktop executable layout is unsupported or redirected.") from None


def inspect_bundle(path: Path | str) -> dict:
    """Read non-secret bundle metadata and validate the official signing identity."""
    require_macos()
    try:
        bundle = Path(path).expanduser().resolve(strict=True)
        info_path = bundle / "Contents/Info.plist"
        if (bundle.suffix != ".app" or not bundle.is_dir() or info_path.is_symlink()
                or not info_path.is_file() or info_path.stat().st_size > 1024 * 1024):
            raise ValueError()
        with info_path.open("rb") as stream:
            info = plistlib.load(stream)
        executable_name = info.get("CFBundleExecutable")
        if (info.get("CFBundleIdentifier") != BUNDLE_ID or not isinstance(executable_name, str)
                or not executable_name or executable_name in (".", "..")
                or "/" in executable_name or "\\" in executable_name):
            raise ValueError()
        version = info.get("CFBundleShortVersionString")
        if not isinstance(version, str) or not version or len(version) > 128:
            raise ValueError()
        executable = _inside_file(bundle, Path("Contents/MacOS") / executable_name)
        real_cli = _inside_file(bundle, CLI_RESOURCE)
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        raise SafeError("Select a supported installed Codex Desktop bundle with identifier com.openai.codex.") from None
    requirement = (f'identifier "{BUNDLE_ID}" and anchor apple generic '
                   f'and certificate leaf[subject.OU] = "{TEAM_ID}"')
    # As in the official Desktop bootstrap, verify nested code and the pinned
    # signer together. '=' selects inline requirement source; without it -R
    # interprets the argument as a requirements filename.
    verified = _command(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", "=" + requirement, str(bundle)])
    if verified.returncode:
        raise SafeError("Desktop signature verification failed. Reinstall the official app; signing and quarantine settings were not changed.")
    details = _command(["/usr/bin/codesign", "-dv", "--verbose=4", str(bundle)])
    fields = {}
    for line in (details.stdout + "\n" + details.stderr).splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"Identifier", "TeamIdentifier"}:
            if key in fields:
                raise SafeError("Desktop signing identity is ambiguous.")
            fields[key] = value
    if details.returncode or fields != {"Identifier": BUNDLE_ID, "TeamIdentifier": TEAM_ID}:
        raise SafeError("Desktop must have the official OpenAI application and signing identity.")
    return {"app_bundle": str(bundle), "bundle_id": BUNDLE_ID, "team_id": TEAM_ID,
            "real_cli": str(real_cli), "app_executable": str(executable),
            "cli_sha256": file_hash(real_cli), "app_version": version}


def discover_bundle(path: Path | str | None = None) -> dict:
    require_macos()
    if path:
        return inspect_bundle(path)
    candidates = [base / name for base in (Path("/Applications"), Path.home() / "Applications")
                  for name in ("Codex.app", "ChatGPT.app") if (base / name).is_dir()]
    candidates = list(dict.fromkeys(candidate.resolve() for candidate in candidates))
    if len(candidates) != 1:
        raise SafeError("Specify the installed app bundle explicitly: no unique Codex.app or ChatGPT.app was found.")
    return inspect_bundle(candidates[0])


def verify_desktop(config: dict) -> dict:
    """Fail closed after an app update, redirected executable, or identity drift."""
    expected = config.get("desktop")
    if not isinstance(expected, dict):
        raise SafeError("Desktop settings are missing. Run Desktop setup first.")
    current = inspect_bundle(expected.get("app_bundle", ""))
    if any(expected.get(key) != value for key, value in current.items()):
        raise SafeError("Desktop installation changed since setup. Verify and configure the new version before launching the bridge.")
    profile = Path(expected.get("codex_home") or Path.home() / ".codex").expanduser()
    if not profile.is_absolute() or not profile.is_dir():
        raise SafeError("The existing Desktop profile directory is unavailable. Open and sign in to Desktop normally first.")
    return dict(current, codex_home=str(profile.resolve()))


def _process_table() -> list[tuple[int, int, str]]:
    # comm contains executable names/paths, never arguments or environment.
    result = _command(["/bin/ps", "-ww", "-axo", "pid=,uid=,comm="])
    if result.returncode:
        raise SafeError("Could not verify whether Desktop is already running.")
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise SafeError("The macOS process identity listing was not understood.")
        rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def process_path(pid: int) -> Path:
    """Use the native executable identity, not a process name or command line."""
    require_macos()
    if type(pid) is not int or pid <= 0:
        raise SafeError("Invalid parent process identity.")
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        function = library.proc_pidpath
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        count = function(pid, buffer, len(buffer))
        if count <= 0 or count >= len(buffer):
            raise ValueError()
        value = os.fsdecode(buffer.value)
        if not Path(value).is_absolute():
            raise ValueError()
        return Path(value).resolve(strict=True)
    except (OSError, ValueError, AttributeError):
        raise SafeError("Could not establish the native Desktop process identity.") from None


def running_desktop(config: dict) -> list[int]:
    require_macos()
    expected = Path(config["desktop"]["app_executable"]).resolve(strict=True)
    found = []
    for pid, uid, command in _process_table():
        if uid != os.getuid():
            continue
        # Ignore unrelated processes, even if they have the same short name.
        if command == str(expected):
            try:
                if process_path(pid) == expected:
                    found.append(pid)
            except SafeError:
                raise SafeError("A possible Desktop process could not be verified. Close Desktop normally and retry.") from None
    return found


def validate_parent(config: dict, parent_pid: int | None = None) -> bool:
    """The gateway is allowed only as a child of this verified Desktop app."""
    identity = verify_desktop(config)
    pid = os.getppid() if parent_pid is None else parent_pid
    parents = [(entry_pid, uid) for entry_pid, uid, _ in _process_table() if entry_pid == pid]
    if parents != [(pid, os.getuid())] or process_path(pid) != Path(identity["app_executable"]):
        raise SafeError("Gateway launch refused: the parent is not this user's verified Desktop application.")
    return True


def secure_home(home: Path) -> Path:
    home = Path(home).expanduser()
    if not home.is_absolute() or home.is_symlink():
        raise SafeError("Use an absolute, unredirected directory for local bridge settings.")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if hasattr(os, "getuid") and home.stat().st_uid != os.getuid():
        raise SafeError("Bridge settings must be owned by the current user.")
    os.chmod(home, 0o700)
    return home.resolve()


@contextmanager
def _launch_lock(home: Path):
    import fcntl
    descriptor = os.open(home / ".desktop-launch.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SafeError("Another Desktop launch or rollback is already in progress.") from None
        yield
    finally:
        os.close(descriptor)


def prepare_launch(config: dict, home: Path) -> Path:
    identity = verify_desktop(config)
    home = secure_home(home)
    # A macOS virtualenv interpreter is commonly a symlink. Resolving it to the
    # base interpreter loses that environment's installed gateway package.
    python = Path(os.path.abspath(sys.executable))
    if not python.is_file():
        raise SafeError("The installation's Python interpreter is unavailable.")
    shim = home / "desktop-engine-shim.sh"
    if shim.is_symlink():
        raise SafeError("Refusing a redirected local gateway launcher.")
    arguments = [str(python), "-m", "codex_telegram.desktop_gateway", "--home", str(home),
                 "--real-cli", identity["real_cli"], "--"]
    content = '#!/bin/sh\nexec ' + " ".join(shlex.quote(arg) for arg in arguments) + ' "$@"\n'
    descriptor, temporary = tempfile.mkstemp(dir=home, prefix=".shim-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o700)
        os.replace(temporary, shim)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    atomic_json(home / ".native-launch.json", {
        "schema": 1, "desktop": identity, "python": str(python),
        "shim": str(shim), "shim_sha256": file_hash(shim),
        "gateway_module": "codex_telegram.desktop_gateway",
    })
    return shim


def launch_environment(config: dict, shim: Path | None) -> dict[str, str]:
    # Keep ordinary user preferences, but never hand bot credentials/API keys or
    # another chat's internal endpoint context to the Desktop process.
    removed = {"CODEX_CLI_PATH", "CODEX_THREAD_ID", "CODEX_APP_TOOLS_PIPE_PATH",
               "CODEX_MCP_NODE_PATH", "ELECTRON_RUN_AS_NODE", "PYTHONPATH", "PYTHONHOME",
               "NODE_OPTIONS", "CODEX_TELEGRAM_HOME"}
    environment = {key: value for key, value in os.environ.items()
                   if key not in removed and not _SECRET_ENV.search(key)
                   and not key.startswith(("DYLD_", "CTD_"))}
    environment["CODEX_HOME"] = str(Path(config["desktop"]["codex_home"]).expanduser().resolve())
    if shim is not None:
        environment["CODEX_CLI_PATH"] = str(shim)
    return environment


def launch(config: dict, home: Path, *, rollback: bool = False) -> dict:
    require_macos()
    identity = verify_desktop(config)
    home = secure_home(home)
    with _launch_lock(home):
        if running_desktop(config):
            raise SafeError("Desktop is already running. Close it normally after saving work, then retry. No process was stopped.")
        shim = None if rollback else prepare_launch(config, home)
        # Recheck after setup work; an existing GUI does not receive a new env.
        if running_desktop(config):
            raise SafeError("Desktop started during preparation. Close it normally and retry.")
        environment = launch_environment(dict(config, desktop=identity), shim)
        process = subprocess.Popen([identity["app_executable"]], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=environment, cwd=str(Path(identity["app_bundle"]).parent),
                                   start_new_session=True, close_fds=True)
    return {"launch_requested": True, "pid": process.pid, "gateway_requested": not rollback,
            "desktop_connection_verified": False}

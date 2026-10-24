"""Set up private Docker relay files and run the existing native Desktop adapter.

No credentials are read from the Desktop profile or exported from native key stores.
Secrets are supplied interactively and are never arguments or environment values.
"""
from __future__ import annotations

import argparse
import getpass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "codex-telegram-docker/1"
CLOUDFLARED = "cloudflare/cloudflared:2026.10.3@sha256:faa8e0a1475f137c910be6025f43c8e4092fdd35461b31e19fd1390657a1ff39"
SECRET_NAMES = ("telegram_bot_token", "connector_key", "cloudflare_tunnel_token")


class SetupError(Exception):
    pass


def host_kind():
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        if sys.version_info[:2] != (3, 12):
            raise SetupError("The Windows native adapter requires Python 3.12.")
        return "windows"
    raise SetupError("Existing Desktop Docker mode requires a native macOS or Windows host.")


def default_home():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "codex-telegram-docker"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise SetupError("LOCALAPPDATA is unavailable.")
        return Path(local) / "CodexTelegramDocker"
    raise SetupError("Run setup on the computer with the installed Desktop app.")


def no_redirect(path):
    path = Path(path).expanduser().absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise SetupError("Redirected settings paths are not supported.")
    return path.resolve()


def private_home(path):
    path = no_redirect(path)
    if path == Path(path.anchor) or path == Path.home().resolve() or path.is_relative_to(ROOT):
        raise SetupError("Choose a separate private data directory outside the downloaded repository.")
    if ".codex" in path.parts or ROOT.is_relative_to(path):
        raise SetupError("The Desktop profile and repository parents cannot be Docker data directories.")
    return path


@lru_cache(maxsize=1)
def windows_sid():
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10)
    match = re.search(r"S-1-\d+(?:-\d+)+", result.stdout)
    if result.returncode or not match:
        raise SetupError("Cannot identify the Windows owner for the private directory ACL.")
    return match.group()


def check_windows_acl(path):
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                              ctypes.POINTER(pointer), ctypes.POINTER(pointer),
                                              ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    security.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
    security.GetAce.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer

    def sid_text(value):
        result = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(value, ctypes.byref(result)):
            raise SetupError("Cannot verify the private Windows ACL.")
        try:
            return result.value
        finally:
            kernel.LocalFree(ctypes.cast(result, pointer))

    class ACL(ctypes.Structure):
        _fields_ = [("revision", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte), ("size", ctypes.c_ushort),
                    ("count", ctypes.c_ushort), ("reserved2", ctypes.c_ushort)]

    owner, dacl, descriptor = pointer(), pointer(), pointer()
    code = security.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None,
                                          ctypes.byref(descriptor))
    if code:
        raise SetupError("Cannot verify ownership and permissions of the private Windows data.")
    try:
        expected = windows_sid()
        if not dacl.value or sid_text(owner) != expected:
            raise SetupError("Private Windows data must be owned by the current user with an explicit DACL.")
        acl = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
        if not acl.count:
            raise SetupError("Private Windows data has an unexpected ACL.")
        for index in range(acl.count):
            ace = pointer()
            if not security.GetAce(dacl, index, ctypes.byref(ace)):
                raise SetupError("Cannot verify the private Windows ACL.")
            # Setup creates only ordinary ACCESS_ALLOWED_ACE entries for this user.
            if ctypes.c_ubyte.from_address(ace.value).value != 0 or sid_text(pointer(ace.value + 8)) != expected:
                raise SetupError("Private Windows data grants permissions to another account. Restore owner-only access before continuing.")
    finally:
        kernel.LocalFree(descriptor)


def set_private_windows_acl(path):
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                            ctypes.POINTER(pointer), ctypes.POINTER(wintypes.DWORD)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    security.GetSecurityDescriptorDacl.argtypes = [pointer, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL)]
    security.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    security.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, pointer, pointer, pointer, pointer]
    security.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    descriptor, dacl = pointer(), pointer()
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    # Protected DACL, exactly one inheritable full-control ACE. This replaces,
    # rather than merges with, explicit ACEs created by Python/Windows defaults.
    sddl = "D:P(A;OICI;FA;;;" + windows_sid() + ")"
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise SetupError("Cannot create the private Windows ACL.")
    try:
        if not security.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present.value:
            raise SetupError("Cannot create the private Windows ACL.")
        if security.SetNamedSecurityInfoW(str(path), 1, 4 | 0x80000000, None, None, dacl, None):
            raise SetupError("Cannot restrict the private directory to the current Windows user.")
    finally:
        kernel.LocalFree(descriptor)


def protect_directory(path):
    # Exclusive creation prevents existing explicit Windows ACEs from surviving.
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.name == "nt":
        set_private_windows_acl(path)
        check_windows_acl(path)
    else:
        path.chmod(0o700)


def write_new_private(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def read_private_json(path):
    path = Path(path)
    no_redirect(path)
    check_windows_acl(path)
    meta = path.stat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > 131072:
        raise SetupError("Invalid private configuration file.")
    if os.name != "nt" and (meta.st_uid != os.getuid() or meta.st_mode & 0o077):
        raise SetupError("Private configuration must belong to this user and have mode 0600.")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise SetupError("Invalid private configuration.")
    return result


def validate_origin(value):
    if not isinstance(value, str):
        raise SetupError("Invalid public HTTPS origin.")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise SetupError("Use the public HTTPS origin without a path, credentials, query or fragment.")
    return value.rstrip("/")


def validate_image(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{1,255}", value):
        raise SetupError("Invalid image reference.")
    last = value.rsplit("/", 1)[-1]
    if ":" not in last or value.endswith(":latest") or "package-check" in value:
        raise SetupError("Use the published runtime candidate version or digest, never latest or package-check.")
    if "@" in value and not re.fullmatch(r".+@sha256:[a-f0-9]{64}", value):
        raise SetupError("Invalid image digest.")
    return value


def native_home(kind):
    if kind == "macos":
        return Path(os.environ.get("CODEX_TELEGRAM_HOME") or Path.home() / "Library" / "Application Support" / "codex-telegram-desktop")
    return Path(os.environ.get("CTD_DATA_DIR") or Path(os.environ["LOCALAPPDATA"]) / "CodexTelegramDesktop")


def native_metadata(kind, home):
    path = no_redirect(home) / "config.json"
    # This is the adapter's non-secret settings file, not a Desktop account profile.
    if not path.is_file() or path.stat().st_size > 131072:
        raise SetupError("Configure the native Desktop adapter first with desktop-setup.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SetupError("Invalid native adapter configuration.")
    if kind == "macos":
        if value.get("backend") != "desktop":
            raise SetupError("The selected native configuration is not an existing-Desktop adapter.")
        owner = value.get("owner_id")
        origin = validate_origin(value.get("public_url", ""))
    else:
        if value.get("schema") != "codex-telegram-desktop/1":
            raise SetupError("Invalid Windows native adapter configuration.")
        owner = value.get("owner_user_id")
        origin = None
    if type(owner) is not int or owner <= 0:
        raise SetupError("The native adapter owner must be a positive numeric Telegram ID.")
    return owner, origin


def run_checked(args, *, env=None, capture=False, timeout=600):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=capture, text=True, timeout=timeout)
    if result.returncode:
        raise SetupError("Command failed. No secret values were included in its arguments.")
    return result.stdout if capture else ""


def docker_available():
    executable = shutil.which("docker")
    if not executable:
        raise SetupError("Install and open Docker Desktop, then retry. This helper does not install it.")
    if run_checked([executable, "info", "--format", "{{.OSType}}"], capture=True, timeout=30).strip() != "linux":
        raise SetupError("Docker Desktop must use Linux containers.")
    run_checked([executable, "compose", "version"], capture=True, timeout=30)
    return executable


def setup(home):
    kind = host_kind()
    home = private_home(home)
    if home.exists():
        raise SetupError("The private data directory already exists. Setup requires a new directory and never overwrites it.")
    native = no_redirect(native_home(kind))
    owner, origin = native_metadata(kind, native)
    docker_available()
    if origin is None:
        origin = validate_origin(input("Panel public HTTPS origin: ").strip())
    image = validate_image(input("Published runtime image version or digest: ").strip())
    print("The container is trusted: its bot token can authenticate Telegram owner requests. Native scope and permissions still apply.")
    print("Use the same BotFather token already configured in the native adapter. It is not read or exported from Keychain/DPAPI.")
    bot = getpass.getpass("BotFather token (hidden): ").strip()
    if not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,100}", bot):
        raise SetupError("Invalid bot token format. No private files were written.")
    tunnel = getpass.getpass("Your Cloudflare named-tunnel token (hidden): ").strip()
    if not re.fullmatch(r"[A-Za-z0-9._=-]{32,32768}", tunnel):
        raise SetupError("Invalid tunnel token format. No private files were written.")
    uid, gid = (os.getuid(), os.getgid()) if kind == "macos" else (10001, 10001)
    if uid == 0 or gid == 0:
        raise SetupError("Run setup as your ordinary user, not root.")
    protect_directory(home)
    protect_directory(home / "secrets")
    protect_directory(home / "state")
    write_new_private(home / "secrets" / "telegram_bot_token", bot + "\n")
    write_new_private(home / "secrets" / "connector_key", secrets.token_hex(32) + "\n")
    write_new_private(home / "secrets" / "cloudflare_tunnel_token", tunnel + "\n")
    settings = {"schema": SCHEMA, "host": kind, "native_home": str(native), "owner_id": owner,
                "public_url": origin, "image": image, "cloudflared_image": CLOUDFLARED, "uid": uid, "gid": gid}
    write_new_private(home / "settings.json", json.dumps(settings, indent=2) + "\n")
    print("Private setup saved outside the repository. No container or Telegram process was started.")
    print("In Cloudflare, route the chosen public hostname to HTTP service http://relay:8787.")
    print("Next: pull, check, up, desktop-launch, desktop-doctor, then host in its own terminal.")


def load_settings(home):
    home = private_home(home)
    check_windows_acl(home)
    value = read_private_json(home / "settings.json")
    if value.get("schema") != SCHEMA or value.get("host") != host_kind():
        raise SetupError("This setup belongs to a different platform or schema.")
    if type(value.get("uid")) is not int or value["uid"] <= 0 or type(value.get("gid")) is not int or value["gid"] <= 0:
        raise SetupError("A non-root numeric container UID/GID is required.")
    value["public_url"] = validate_origin(value.get("public_url", ""))
    value["image"] = validate_image(value.get("image", ""))
    value["cloudflared_image"] = validate_image(value.get("cloudflared_image", ""))
    if not isinstance(value.get("native_home"), str) or not Path(value["native_home"]).is_absolute():
        raise SetupError("Invalid native adapter settings directory.")
    owner, origin = native_metadata(value["host"], Path(value["native_home"]))
    if owner != value.get("owner_id") or origin not in (None, value["public_url"]):
        raise SetupError("Native owner/public origin changed. Review the private Docker setup before continuing.")
    for name in SECRET_NAMES:
        path = home / "secrets" / name
        no_redirect(path)
        check_windows_acl(path)
        meta = path.stat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or not 1 <= meta.st_size <= 32769:
            raise SetupError("Missing or invalid secret file.")
        if os.name != "nt" and (meta.st_uid != os.getuid() or meta.st_mode & 0o077):
            raise SetupError("Secret files must belong to this user and have mode 0600.")
    state = no_redirect(home / "state")
    check_windows_acl(state)
    if not state.is_dir():
        raise SetupError("The private Docker state directory is unavailable.")
    return value


def compose_environment(home, value):
    env = os.environ.copy()
    env.update(CTD_DOCKER_HOME=home.as_posix(), CTD_IMAGE=value["image"],
               CTD_CLOUDFLARED_IMAGE=value["cloudflared_image"], CTD_PUBLIC_URL=value["public_url"],
               CTD_OWNER_ID=str(value["owner_id"]), CTD_UID=str(value["uid"]), CTD_GID=str(value["gid"]))
    return env


def relay_args(value):
    return ["--public-url", value["public_url"], "--owner-id", str(value["owner_id"]),
            "--token-file", "/run/secrets/telegram_bot_token", "--key-file", "/run/secrets/connector_key",
            "--state-dir", "/data"]


def native_command(kind, action):
    if kind == "macos":
        return [sys.executable, "-m", "codex_telegram.macos_cli", action]
    base = ROOT / "adapters" / "windows_desktop"
    if action in ("launch", "rollback"):
        return [sys.executable, str(base / "gateway" / "activate.py"), *(["--rollback"] if action == "rollback" else [])]
    filename = {"setup": "setup.py", "run": "start.py", "doctor": "doctor.py"}[action]
    return [sys.executable, str(base / "scripts" / filename), *(["--native"] if action == "doctor" else [])]


def host_environment(home, value):
    env = os.environ.copy()
    env.update(CTD_RELAY_KEY_FILE=str(home / "secrets" / "connector_key"), CTD_RELAY_PUBLIC_URL=value["public_url"])
    env["CODEX_TELEGRAM_HOME" if value["host"] == "macos" else "CTD_DATA_DIR"] = value["native_home"]
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(description="Docker relay and native Desktop host; secrets stay outside this repository.")
    parser.add_argument("command", choices=("setup", "pull", "check", "up", "down", "status", "host", "desktop-setup", "desktop-launch", "desktop-doctor", "desktop-rollback"))
    parser.add_argument("--home", type=Path, help="Separate private Docker data directory; never the Desktop profile")
    args = parser.parse_args(argv)
    try:
        if args.command == "desktop-setup":
            kind = host_kind()
            if (native_home(kind) / "config.json").is_file():
                native_metadata(kind, native_home(kind))
                print("Existing native adapter settings retained. Use its own setup tools to change them.")
            else:
                run_checked(native_command(kind, "setup"), timeout=None)
            return 0
        home = private_home(args.home or default_home())
        if args.command == "setup":
            setup(home)
            return 0
        value = load_settings(home)
        if args.command == "host" or args.command.startswith("desktop-"):
            action = "run" if args.command == "host" else args.command.removeprefix("desktop-")
            run_checked(native_command(value["host"], action), env=host_environment(home, value), timeout=None)
            return 0
        docker = docker_available()
        command = [docker, "compose", "--project-directory", str(ROOT), "-f", str(ROOT / "compose.yml")]
        env = compose_environment(home, value)
        if args.command in ("check", "up"):
            run_checked(command + ["run", "--rm", "--no-deps", "relay", *relay_args(value), "--self-check"], env=env)
            if args.command == "check":
                print("Container configuration/secret access checked; Desktop and Telegram were not exercised.")
                return 0
        if args.command == "status":
            run_checked(command + ["ps"], env=env)
            probe = "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=3)); print(json.dumps(r)); raise SystemExit(0 if r.get('host_connected') is True else 2)"
            run_checked(command + ["exec", "-T", "relay", "python", "-c", probe], env=env, timeout=15)
        else:
            operation = {"pull": ["pull"], "up": ["up", "-d"], "down": ["down", "--timeout", "20"]}[args.command]
            run_checked(command + operation, env=env)
        return 0
    except KeyboardInterrupt:
        print("Stopped. The Desktop application was not terminated.")
        return 130
    except (SetupError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(str(exc) if isinstance(exc, SetupError) else "Local operation failed; check prerequisites and private file permissions. No credentials were printed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

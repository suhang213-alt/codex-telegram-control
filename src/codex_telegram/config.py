"""Explicit local configuration. Credentials never enter the repository."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from urllib.parse import urlsplit


class SafeError(Exception):
    """A fixed, non-sensitive error suitable for displaying to the owner."""


def data_home() -> Path:
    override = os.environ.get("CODEX_TELEGRAM_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return base / "codex-telegram-control"


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise SafeError("Refusing a redirected settings file.")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".write-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate(raw: dict) -> dict:
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise SafeError("Unsupported configuration. Run setup.")
    owner = raw.get("owner_id")
    if type(owner) is not int or owner <= 0 or owner >= 2**53:
        raise SafeError("A positive numeric Telegram owner ID is required.")
    workspace = Path(raw.get("workspace", "")).expanduser()
    if not workspace.is_absolute() or not workspace.is_dir():
        raise SafeError("Workspace must be an existing absolute directory.")
    public = raw.get("public_url", "").rstrip("/")
    url = urlsplit(public)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ("", "/")):
        raise SafeError("Panel URL must be an HTTPS origin without a path or credentials.")
    if url.hostname in ("localhost", "127.0.0.1", "::1"):
        raise SafeError("Telegram needs your reachable HTTPS address.")
    port = raw.get("port", 8787)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise SafeError("Invalid local port.")
    codex = raw.get("codex_command", ["codex"])
    if not isinstance(codex, list) or not codex or any(not isinstance(x, str) or not x or "\x00" in x for x in codex):
        raise SafeError("codex_command must be a non-empty argument array.")
    sandbox = raw.get("sandbox", "read-only")
    if sandbox not in ("read-only", "workspace-write"):
        raise SafeError("Only read-only and workspace-write modes are supported.")
    return dict(raw, workspace=str(workspace.resolve()), public_url=public, port=port,
                codex_command=codex, sandbox=sandbox)


def load(home: Path) -> dict:
    try:
        return validate(json.loads((home / "config.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        raise SafeError("Configuration unavailable. Run codex-telegram setup.") from None


def secure_keyring():
    try:
        import keyring
        backend = keyring.get_keyring()
        name = type(backend).__module__
        # Fail closed: no plaintext, null, fail, or chained fallback backends.
        if name not in ("keyring.backends.Windows", "keyring.backends.macOS",
                        "keyring.backends.SecretService", "keyring.backends.kwallet"):
            raise SafeError("No approved OS keyring. Set TELEGRAM_BOT_TOKEN for this process instead.")
        return keyring
    except ImportError:
        raise SafeError("Install package dependencies or set TELEGRAM_BOT_TOKEN for this process.") from None


def token(home: Path) -> str:
    value = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not value:
        try:
            value = secure_keyring().get_password("codex-telegram-control", str(home.resolve()))
        except SafeError:
            raise
        except Exception:
            raise SafeError("OS credential store is locked or unavailable.") from None
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,100}", value):
        raise SafeError("A valid bot token is required; use setup or TELEGRAM_BOT_TOKEN.")
    return value


def resolve_command(command: list[str]) -> list[str]:
    executable = shutil.which(command[0]) or (command[0] if Path(command[0]).is_file() else None)
    if not executable:
        raise SafeError("Codex executable not found. Install Codex CLI and run setup again.")
    if Path(executable).suffix.lower() in (".cmd", ".bat"):
        # No cmd.exe or shell interpolation; use the npm script through Node.
        node = shutil.which("node")
        script = Path(executable).parent / "node_modules/@openai/codex/bin/codex.js"
        if not node or not script.is_file():
            raise SafeError("Use a native Codex executable or a standard npm Codex installation.")
        return [node, str(script), *command[1:]]
    return [executable, *command[1:]]

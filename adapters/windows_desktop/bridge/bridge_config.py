"""Local-only settings for the optional Windows Desktop adapter.

Importing this module performs no I/O writes and never contacts a service.
Secrets and runtime data live outside the repository under LOCALAPPDATA.
"""
from __future__ import annotations
import bridge_config as config
import json
import os
from pathlib import Path
import uuid


def data_dir():
    override = os.environ.get("CTD_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "CodexTelegramDesktop"


def settings_path():
    return data_dir() / "config.json"


def settings():
    path = settings_path()
    if not path.is_file():
        return {}
    if path.stat().st_size > 65536:
        raise ValueError("configuration_too_large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "codex-telegram-desktop/1":
        raise ValueError("configuration_schema_invalid")
    return value


def runtime_dir():
    return data_dir() / "runtime"


def codex_home():
    return Path(settings().get("codex_home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def service_thread_id():
    value = settings().get("service_thread_id", "")
    if value:
        uuid.UUID(value)
    return value


def owner_user_id():
    value = settings().get("owner_user_id")
    if type(value) is not int or not 0 < value < 2**52:
        return None
    return value


def owner_matches(user):
    return isinstance(user, dict) and type(user.get("id")) is int and user["id"] == owner_user_id()


def allowed_workspace(path):
    if not isinstance(path, (str, Path)) or not str(path):
        return False
    try:
        candidate = Path(path).expanduser().resolve(strict=True)
        for item in settings().get("allowed_workspaces", []):
            root = Path(item).expanduser().resolve(strict=True)
            if candidate == root or candidate.is_relative_to(root):
                return True
    except (OSError, ValueError, TypeError):
        return False
    return False


def plugin_dir():
    """Explicit plugin selection or unique installed bundled server only."""
    configured = settings().get("plugin_dir")
    if configured:
        return Path(configured).expanduser().resolve()
    root = codex_home() / "plugins/cache/openai-bundled/codex-app-tools"
    candidates = [p.parent for p in root.glob("*/server.mjs") if p.is_file()]
    # Ambiguous cached versions require selection; newest is not proof of compatibility.
    return candidates[0] if len(candidates) == 1 else data_dir() / "unconfigured-plugin"


def thread_allowed(identifier):
    """Only verified local metadata can grant access; client cwd never can."""
    import sqlite3
    from contextlib import closing
    if not isinstance(identifier, str) or identifier == service_thread_id():
        return False
    try:
        uuid.UUID(identifier)
        databases = list(codex_home().glob("state_*.sqlite"))
        databases.sort(key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1)
        if not databases:
            return False
        with closing(sqlite3.connect(databases[-1].as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT cwd FROM threads WHERE id=?", (identifier,)).fetchone()
        return bool(row and allowed_workspace(row[0]))
    except (ValueError, OSError, sqlite3.Error):
        return False


def project_allowed(row):
    if not isinstance(row, dict) or "chatgpt" in str(row.get("projectKind", "")).casefold():
        return False
    return allowed_workspace(row.get("path") or row.get("cwd") or row.get("rootPath"))


def validate(value):
    if value.get("schema") != "codex-telegram-desktop/1":
        raise ValueError("configuration_schema_invalid")
    owner = value.get("owner_user_id")
    if type(owner) is not int or not 0 < owner < 2**52:
        raise ValueError("owner_id_invalid")
    uuid.UUID(value.get("service_thread_id", ""))
    workspaces = value.get("allowed_workspaces")
    if not isinstance(workspaces, list) or not workspaces:
        raise ValueError("workspace_required")
    for path in workspaces:
        if not Path(path).is_absolute() or not Path(path).is_dir():
            raise ValueError("workspace_unavailable")
    if value.get("tunnel_provider", "none") not in {"none", "localhost.run"}:
        raise ValueError("tunnel_provider_invalid")
    if value.get("compatibility_acknowledged") is not True:
        raise ValueError("desktop_compatibility_acknowledgement_required")
    return value


def write_settings(value):
    validate(value)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)

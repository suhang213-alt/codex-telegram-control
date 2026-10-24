"""Positive, metadata-only scope for Telegram: Codex or proven ChatGPT Work.

The native list/read APIs expose backing kind, not ChatGPT mode. A title,
project, message, or client-supplied isWork flag is never evidence of Work.
"""
from __future__ import annotations
import json
import os
import re
import tempfile
from pathlib import Path

PROVENANCE = "native_create_thread_chatgptWorkCloud"
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")

def _validate(value):
    if not isinstance(value,dict) or set(value)!={"threadId","provenance"}:
        raise ValueError("invalid_work_proof")
    if not isinstance(value["threadId"],str) or not UUID.fullmatch(value["threadId"]):
        raise ValueError("invalid_work_thread_id")
    if value["provenance"]!=PROVENANCE:
        raise ValueError("invalid_work_provenance")
    return value["threadId"]

def load_ids(directory):
    """Malformed/missing proof fails closed; never read any chat content."""
    directory=Path(directory)
    result=set()
    if not directory.is_dir():
        return result
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_size>512:
                continue
            identifier=_validate(json.loads(path.read_text(encoding="utf-8")))
            if path.name==identifier+".json":
                result.add(identifier)
        except (OSError,ValueError,TypeError):
            continue
    return result

def register(directory,payload):
    """Call only after confirmed native creation with target chatgptWorkCloud.

    One atomic file per exact ID avoids lost updates between worker processes.
    Nothing is registered by Telegram text, callback, title or project label.
    """
    identifier=_validate(payload)
    directory=Path(directory)
    if identifier in load_ids(directory):
        return False
    directory.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=directory,
                prefix=".work-",suffix=".tmp",delete=False) as target:
            temporary=Path(target.name)
            json.dump(payload,target,separators=(",",":"))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary,directory/(identifier+".json"))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return True

def allowed(row,work_ids):
    if not isinstance(row,dict) or not isinstance(row.get("id"),str) or not row["id"]:
        return False
    kind=row.get("kind")
    import bridge_config as config
    return kind == "codex" and config.thread_allowed(row["id"])

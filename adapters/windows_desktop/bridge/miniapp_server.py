"""Owner-only HTTPS-backed Telegram Mini App API.

The server listens on localhost. A temporary tunnel provides public HTTPS.
Mini App APIs require Telegram initData; the two cloud delivery routes use a
separate enrolled device key and authenticated encryption instead.
"""
from __future__ import annotations
import bridge_config as config

import cgi
from datetime import datetime
import hashlib
import gzip
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from urllib.parse import quote
from urllib.parse import parse_qsl, unquote, urlsplit
import urllib.request

import artifact_results
import telegram_bridge as bridge


HOST, PORT = "127.0.0.1", 8787
STATIC = Path(__file__).with_name("miniapp")
FILES = bridge.SECRET_DIR / "attachments"
FILE_INDEX = bridge.APP_DIR / "attachments_index.bin"
ARTIFACT_FILES = bridge.SECRET_DIR / "artifact_results"
REQUEST_INDEX = bridge.APP_DIR / "miniapp_requests.bin"
REQUEST_LOCK = threading.Lock()
FILE_LOCK = threading.Lock()
VIEW_LOCK = threading.Lock()
VIEW_AT = {}
HISTORY_LOCK = threading.Lock()
HISTORY_STATE = {}
HISTORY_PAGE_SIZE = 30
HISTORY_PAGE_BYTES = 192000
DELEGATION_SCAN_BYTES = 32 * 1024 * 1024
DELEGATION_ITEM_BYTES = 128 * 1024
DELEGATION_RECEIPT_LOCK = threading.Lock()
MAX_UPLOAD = 20_000_000
ALLOWED = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".mp4", ".mov", ".webm", ".txt", ".csv"}
MODEL_EFFORTS = bridge.MODEL_EFFORTS
USAGE_SERVICE_LOCK = threading.Lock()
_USAGE_SERVICE = None
TASK_PROGRESS_SERVICE_LOCK = threading.Lock()
_TASK_PROGRESS_SERVICE = None
TASK_PROGRESS_SCAN_LOCK = threading.Lock()
_TASK_PROGRESS_SCAN_OWNER = None
_TASK_PROGRESS_CATALOG = {}
TUNNEL_FAILURE_LIMIT = 8
TUNNEL_OUTAGE_GRACE_SECONDS = 120
TUNNEL_LIFECYCLE_LOCK = threading.Lock()


def get_task_progress_service():
    """Local snapshots only. Never enqueue native/model work from a progress GET."""
    global _TASK_PROGRESS_SERVICE
    with TASK_PROGRESS_SERVICE_LOCK:
        if _TASK_PROGRESS_SERVICE is None:
            from all_task_progress import AllTaskProgress
            from task_progress import current, project
            _TASK_PROGRESS_SERVICE = AllTaskProgress(legacy_provider=current, projector=project)
        return _TASK_PROGRESS_SERVICE


def task_progress_snapshot(thread_id):
    row = next((row for row in bridge.cached_threads() if row.get('id') == thread_id), None)
    return get_task_progress_service().snapshot(thread_id, row=row)


def task_progress_batch(scope='active'):
    if scope not in ('active', 'all'):
        raise ValueError('invalid_progress_scope')
    global _TASK_PROGRESS_SCAN_OWNER, _TASK_PROGRESS_CATALOG
    service = get_task_progress_service()
    cached = bridge.cached_threads()  # Already passed existing backing-kind/Work scope.
    trusted_ids = {row['id'] for row in cached if isinstance(row, dict) and isinstance(row.get('id'), str)}
    rows = service.discover(cached)  # Metadata only, not rollout content.
    denied = 0
    allowed = []
    for row in rows[:256]:
        if row.get('id') not in trusted_ids and not bridge.thread_in_scope(row.get('id')):
            denied += 1
        else:
            allowed.append(row)
    truncated = len(rows) > 256 or service.discovery_truncated is True
    with TASK_PROGRESS_SCAN_LOCK:
        if _TASK_PROGRESS_SCAN_OWNER is not service:
            _TASK_PROGRESS_CATALOG = {}
            _TASK_PROGRESS_SCAN_OWNER = service
        valid_ids = {row['id'] for row in allowed}
        _TASK_PROGRESS_CATALOG = {key: value for key, value in _TASK_PROGRESS_CATALOG.items() if key in valid_ids}
        def revision(row):
            return (row.get('updatedAt'), row.get('status'), row.get('kind'), row.get('title'))
        def is_active(row):
            saved = _TASK_PROGRESS_CATALOG.get(row['id'], {}).get('item', {})
            return (str(row.get('status') or '').casefold() in bridge.ACTIVE_STATUSES or
                    str(saved.get('runtime_status') or '').casefold() in bridge.ACTIVE_STATUSES | {'waiting'})
        candidates = [row for row in allowed if is_active(row) or
                      _TASK_PROGRESS_CATALOG.get(row['id'], {}).get('revision') != revision(row) or
                      _TASK_PROGRESS_CATALOG.get(row['id'], {}).get('item', {}).get('available') is not True or
                      _TASK_PROGRESS_CATALOG.get(row['id'], {}).get('item', {}).get('turn_confirmed') is not True]
        def recent(row):
            try:
                value = float(row.get('updatedAt') or 0)
                return value if value == value and abs(value) != float('inf') else 0
            except (TypeError, ValueError):
                return 0
        order = lambda row: (_TASK_PROGRESS_CATALOG.get(row['id'], {}).get('at', 0), -recent(row))
        active_rows = sorted((row for row in candidates if is_active(row)), key=order)
        other_rows = sorted((row for row in candidates if not is_active(row)), key=order)
        chosen = active_rows[:12] + other_rows[:4]
        chosen_ids = {row['id'] for row in chosen}
        chosen += [row for row in sorted(candidates, key=order) if row['id'] not in chosen_ids][:16-len(chosen)]
        for row in chosen:
            identifier = row['id']
            try:
                item = service.snapshot(identifier, row=row)
            except Exception:
                item = {'schema': 'codex-task-progress/1', 'thread_id': identifier,
                        'available': False, 'reason': 'source_unavailable'}
            if not isinstance(item, dict) or item.get('thread_id') != identifier:
                item = {'schema': 'codex-task-progress/1', 'thread_id': identifier,
                        'available': False, 'reason': 'invalid_snapshot'}
            _TASK_PROGRESS_CATALOG[identifier] = {'item': dict(item), 'revision': revision(row), 'at': time.monotonic()}
        items, observed = [], []
        for row in allowed:
            entry = _TASK_PROGRESS_CATALOG.get(row['id'], {})
            if entry.get('revision') == revision(row):
                item = dict(entry['item'])
            else:
                # Never show an old100% while a changed/new task awaits its bounded read.
                item = {'schema': 'codex-task-progress/1', 'thread_id': row['id'], 'title': row.get('title', 'Задача'),
                        'available': True, 'percent': None, 'done': None, 'total': None,
                        'runtime_status': 'unknown', 'runtime_stale': True, 'turn_confirmed': False,
                        'status': 'Ожидает чтения текущего статуса', 'checkpoints': [], 'results': []}
            item['kind'] = row.get('kind', 'codex')
            observed.append(item)
            running = str(item.get('runtime_status') or '').casefold()
            hinted = running == 'unknown' and str(row.get('status') or '').casefold() in bridge.ACTIVE_STATUSES
            if scope == 'all' or running in bridge.ACTIVE_STATUSES | {'waiting'} or hinted:
                items.append(item)
    from datetime import timezone, timedelta
    return {'schema': 'codex-task-progress-batch/1', 'checked_at_msk': datetime.now(timezone(timedelta(hours=3))).isoformat(),
            'items': items, 'scope': scope, 'truncated': truncated,
            'catalog_complete': service.discovery_complete is True and not truncated and not denied and
                all(item.get('available') is True and item.get('turn_confirmed') is True for item in observed),
            'catalog_total': len(allowed), 'catalog_scanned': sum(item.get('turn_confirmed') is True for item in observed)}


def get_usage_service():
    """Lazy owner-only account reader; no model calls or periodic polling."""
    global _USAGE_SERVICE
    with USAGE_SERVICE_LOCK:
        if _USAGE_SERVICE is None:
            from usage_provider import UsageService, read_codex_usage
            _USAGE_SERVICE = UsageService(
                reader=read_codex_usage,
                load_cache=lambda: bridge.read_json(bridge.USAGE_CACHE) if bridge.USAGE_CACHE.exists() else None,
                save_cache=lambda payload: bridge.atomic_json(bridge.USAGE_CACHE, payload),
            )
        return _USAGE_SERVICE


def cloud_push_service():
    """Use internal DPAPI accessors; enrolled device key never leaves this API."""
    from desktop_push import DesktopPush
    from wake_dispatcher import notify_pending
    return DesktopPush(
        key_provider=lambda: bridge.read_json(bridge.SECRET_DIR / "desktop_push_key.bin"),
        state_provider=lambda: bridge.read_json(bridge.STATE_PATH) if bridge.STATE_PATH.is_file() else {},
        process_update=lambda update: bridge.process_update(update, acknowledge_callback=False),
        challenge_path=bridge.SECRET_DIR / "desktop_push_challenges.sqlite3",
        receipt_path=bridge.SECRET_DIR / "desktop_receipts.sqlite3",
        expected_username=bridge.EXPECTED_USERNAME,
        wake=notify_pending,
    )


def verify_init_data(raw: str, *, now: int | None = None) -> bool:
    if not raw or len(raw) > 8192:
        return False
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    if len(pairs) != len({key for key, _ in pairs}):
        return False
    fields = dict(pairs)
    received = fields.pop("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", received):
        return False
    try:
        signed_at = int(fields["auth_date"])
        user = json.loads(fields["user"])
        owner = bridge.state()
        if int(user["id"]) != int(owner["owner_user_id"]):
            return False
        if not config.owner_matches(user):
            return False
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    timestamp = int(time.time()) if now is None else now
    if not (timestamp - 3600 <= signed_at <= timestamp + 60):
        return False
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bridge.token().encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(received.lower(), expected)


def index() -> dict:
    return bridge.read_json(FILE_INDEX) if FILE_INDEX.exists() else {}


def get_file(file_id: str) -> tuple[dict, Path] | None:
    if not re.fullmatch(r"[a-f0-9]{32}", file_id):
        return None
    entry = index().get(file_id)
    if not isinstance(entry, dict):
        return None
    path = FILES / (file_id + entry.get("ext", ""))
    if not path.is_file() or path.resolve().parent != FILES.resolve():
        return None
    return entry, path


def store_file(name: str, content: bytes) -> dict:
    if not content or len(content) > MAX_UPLOAD:
        raise ValueError("Файл должен быть меньше 20 МБ")
    clean = Path(name.replace("\\", "/")).name[:150]
    ext = Path(clean).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError("Этот тип файла не поддерживается")
    with FILE_LOCK:
        FILES.mkdir(parents=True, exist_ok=True)
        file_id = secrets.token_hex(16)
        path = FILES / (file_id + ext)
        path.write_bytes(content)
        record = {"id": file_id, "name": clean, "size": len(content), "mimeType": mimetypes.guess_type(clean)[0] or "application/octet-stream", "ext": ext, "at": int(time.time())}
        rows = index()
        rows[file_id] = record
        bridge.atomic_json(FILE_INDEX, rows)
    return {key: record[key] for key in ("id", "name", "size", "mimeType")}


def thread_rows() -> list:
    return bridge.cached_threads()


def project_rows() -> list:
    payload = bridge.read_json(bridge.PROJECTS_CACHE) if bridge.PROJECTS_CACHE.exists() else {}
    return payload.get("projects", [])


def archived_rows() -> list:
    return bridge.cached_archived()


def local_codex_rollout(thread_id: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", thread_id):
        return None
    root = config.codex_home()
    databases = sorted(root.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1)
    if not databases:
        return None
    try:
        # Desktop updates live thread metadata in WAL. immutable=1 ignores those
        # committed rows and incorrectly sends local chats through native view.
        connection = sqlite3.connect(databases[-1].as_uri() + "?mode=ro", uri=True, timeout=.5)
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT rollout_path FROM threads WHERE id=? AND archived=0 AND thread_source IN "
                "('user','chatgpt_handoff','avatar_quick_chat','agent_forked_thread','agent_created_thread')",
                (thread_id,),
            ).fetchone()
        finally:
            connection.close()
        if not row or not row[0]:
            return None
        path = Path(row[0]).resolve(strict=True)
        root_name = os.path.normcase(str(root.resolve()).removeprefix("\\\\?\\"))
        path_name = os.path.normcase(str(path).removeprefix("\\\\?\\"))
        if os.path.commonpath((root_name, path_name)) != root_name or "sessions" not in path.parts or path.suffix != ".jsonl":
            return None
        return path
    except (OSError, sqlite3.Error, ValueError):
        return None


def local_codex_history(thread_id: str) -> list | None:
    """Read complete user-facing text from the local Codex rollout, incrementally."""
    path = local_codex_rollout(thread_id)
    if path is None:
        return None
    with HISTORY_LOCK:
        entry = HISTORY_STATE.get(thread_id)
        if entry is None or entry["path"] != path or entry["offset"] > path.stat().st_size:
            entry = {"path": path, "offset": 0, "messages": [], "at": time.monotonic()}
            HISTORY_STATE[thread_id] = entry
        with path.open("rb") as source:
            source.seek(entry["offset"])
            while True:
                line = source.readline()
                if not line or not line.endswith(b"\n"):
                    break
                entry["offset"] = source.tell()
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if record.get("type") != "response_item":
                    continue
                item = record.get("payload") or {}
                if item.get("type") != "message":
                    continue
                role = item.get("role")
                if role != "user" and not (role == "assistant" and item.get("phase") in ("commentary", "final", "final_answer")):
                    continue
                parts = [part.get("text", "") for part in item.get("content", []) if isinstance(part, dict) and part.get("type") in ("input_text", "output_text")]
                message = "\n".join(part for part in parts if isinstance(part, str) and part)
                if not message:
                    continue
                meta = item.get("internal_chat_message_metadata_passthrough") or {}
                entry["messages"].append({"id": item.get("id") or "local:" + str(entry["offset"]), "role": role, "text": message, "createdAt": meta.get("create_time"), "attachments": []})
        entry["at"] = time.monotonic()
        if len(HISTORY_STATE) > 8:
            oldest = min(HISTORY_STATE, key=lambda key: HISTORY_STATE[key]["at"])
            if oldest != thread_id:
                HISTORY_STATE.pop(oldest, None)
        return list(entry["messages"])


def reverse_line_ranges(source, end: int):
    """Yield complete JSONL byte ranges newest first, without buffering huge lines."""
    position, line_end = end, end
    while position > 0:
        start = max(0, position - 65536)
        source.seek(start)
        block = source.read(position - start)
        index = len(block)
        while True:
            index = block.rfind(b"\n", 0, index)
            if index < 0:
                break
            boundary = start + index + 1
            if boundary < line_end:
                yield boundary, line_end
                line_end = boundary
        position = start
    if line_end:
        yield 0, line_end


def local_history_page(thread_id: str, before: str | None = None) -> dict | None:
    """Read only the requested tail page; tool-output bodies are never decoded."""
    path = local_codex_rollout(thread_id)
    if path is None:
        return None
    messages, size, cursor = [], 0, None
    with path.open("rb") as source:
        end = path.stat().st_size
        if before is not None:
            match = re.fullmatch(r"local:v2:([0-9]{1,16})", before)
            if not match or int(match[1]) > end:
                raise ValueError("Invalid local history cursor")
            end = int(match[1])
            if end:
                source.seek(end - 1)
                if source.read(1) != b"\n":
                    raise ValueError("Invalid local history cursor")
        # Ignore an in-progress trailing record until its newline is committed.
        if end:
            source.seek(end - 1)
            if source.read(1) != b"\n":
                source.seek(0)
                end = next(reverse_line_ranges(source, end))[0]
        for start, stop in reverse_line_ranges(source, end):
            source.seek(start)
            prefix = source.read(min(stop - start, 2048))
            outer = re.search(rb'"type"\s*:\s*"([^"]+)"', prefix)
            if outer and outer[1] != b"response_item":
                continue
            inner = re.search(rb'"payload"\s*:\s*\{\s*"type"\s*:\s*"([^"]+)"', prefix)
            if inner and inner[1] != b"message":
                continue
            source.seek(start)
            try:
                record = json.loads(source.read(stop - start))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if record.get("type") != "response_item":
                continue
            item = record.get("payload") or {}
            if item.get("type") != "message":
                continue
            role = item.get("role")
            if role != "user" and not (role == "assistant" and item.get("phase") in ("commentary", "final", "final_answer")):
                continue
            parts = [part.get("text", "") for part in item.get("content", []) if isinstance(part, dict) and part.get("type") in ("input_text", "output_text")]
            message = "\n".join(part for part in parts if isinstance(part, str) and part)
            if not message:
                continue
            meta = item.get("internal_chat_message_metadata_passthrough") or {}
            row = {"id": item.get("id") or "local:" + str(stop), "role": role, "text": message,
                   "createdAt": meta.get("create_time") or record.get("timestamp"), "attachments": []}
            row_bytes = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if messages and size + row_bytes > HISTORY_PAGE_BYTES:
                cursor = stop
                break
            messages.append(row)
            size += row_bytes
            if len(messages) == HISTORY_PAGE_SIZE:
                cursor = start or None
                break
    return {"messages": list(reversed(messages)), "nextCursor": "local:v2:" + str(cursor) if cursor else None,
            "historyComplete": cursor is None, "localHistory": True}


def message_timestamp(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 100_000_000_000 else 1)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _uncertain_native_target(operation_id: int) -> str | None:
    """Read the native mutation checkpoint; a timeout is not a failed send."""
    from background_service import LOCAL_DIR

    path = LOCAL_DIR / "operations.sqlite3"
    try:
        if not path.is_file():
            return None
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05) as db:
            row = db.execute(
                "SELECT state,target FROM operations WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return row[1] if row and row[0] == "uncertain" and isinstance(row[1], str) else None


def _native_gateway_checkpoint(operation_id: int) -> dict | None:
    """Read metadata from the exact native operation; never acquire its writer lock."""
    from background_service import LOCAL_DIR

    path = LOCAL_DIR / "operations.sqlite3"
    try:
        if not path.is_file():
            return None
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05)
        try:
            row = db.execute("SELECT state,target,receipt_json FROM operations WHERE operation_id=?",
                             (str(operation_id),)).fetchone()
        finally:
            db.close()
        receipt = json.loads(row[2]) if row else None
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None
    if not isinstance(receipt, dict) or receipt.get("deliveryTransport") != "native_gateway":
        return None
    return {"state": row[0], "target": row[1], "receipt": receipt}


def _exact_delegated_input(path: Path, thread_id: str, prompt_digest: str,
                           submitted_at: float) -> dict | None:
    """Find one complete native delegated input after this answer was submitted.

    Reading is bounded to the selected chat's local rollout. A partial window,
    duplicate, truncated item, wrong source, or wrong text proves nothing.
    """
    from native_delegation import matched_delegated_input_indices
    from native_mcp_client import SERVICE_THREAD_ID

    matches = []
    covered = False
    with path.open("rb") as source:
        end = os.fstat(source.fileno()).st_size
        lower_bound = max(0, end - DELEGATION_SCAN_BYTES)
        for start, stop in reverse_line_ranges(source, end):
            if start < lower_bound:
                break
            size = stop - start
            source.seek(start)
            prefix = source.read(min(size, 512))
            if b"function_call_output" not in prefix:
                continue
            if size > DELEGATION_ITEM_BYTES:
                return None
            source.seek(start)
            try:
                record = json.loads(source.read(size))
            except (ValueError, UnicodeDecodeError, TypeError):
                return None
            if not isinstance(record, dict) or record.get("type") != "response_item":
                continue
            stamp = message_timestamp(record.get("timestamp"))
            if stamp is None:
                return None
            if stamp < submitted_at:
                covered = True
                break
            metadata = record.get("metadata") or {}
            if not isinstance(metadata, dict) or metadata.get("thread_id", thread_id) != thread_id:
                return None
            item = record.get("payload") or {}
            if (not isinstance(item, dict) or
                    item.get("type") != "function_call_output" or
                    item.get("name") != "send_message_to_thread" or
                    item.get("namespace") != "codex_app" or
                    not isinstance(item.get("output"), str)):
                continue
            complete = {**item, "type": "functionCallOutput",
                        "output": {"text": item["output"], "truncated": False}}
            if matched_delegated_input_indices(
                    [complete], source_thread_id=SERVICE_THREAD_ID,
                    prompt_digest=prompt_digest):
                matches.append({"itemId": item["id"], "observedAt": stamp})
                if len(matches) > 1:
                    return None
        else:
            covered = True
    return matches[0] if covered and len(matches) == 1 else None


def _uncertain_decision_delegation(operation_id: int, target: str) -> dict | None:
    """Reconcile an uncertain decision without ever resending its native effect."""
    try:
        in_scope = bridge.thread_in_scope(target)
    except (OSError, ValueError):
        return None
    if not in_scope:
        return None
    pending = bridge.INBOX / f"{operation_id}.bin"
    try:
        if not pending.is_file():
            return None
        record = bridge.read_json(pending)
    except (OSError, ValueError, UnicodeError):
        return None
    if (not isinstance(record, dict) or record.get("id") != operation_id or
            record.get("kind") != "followup" or record.get("decision_answer") is not True or
            record.get("selected_thread_id") != target or
            not isinstance(record.get("text"), str) or not record["text"] or
            type(record.get("received_at")) not in (int, float)):
        return None
    digest = hashlib.sha256(record["text"].encode("utf-8")).hexdigest()
    submitted_at = float(record["received_at"])
    receipt_path = bridge.APP_DIR / "delegation_receipts" / f"{operation_id}.bin"
    try:
        saved = bridge.read_json(receipt_path) if receipt_path.is_file() else None
    except (OSError, ValueError, UnicodeError):
        saved = None
    if (isinstance(saved, dict) and saved.get("threadId") == target and
            saved.get("promptDigest") == digest and saved.get("submittedAt") == submitted_at and
            isinstance(saved.get("itemId"), str) and saved["itemId"] and
            type(saved.get("observedAt")) in (int, float) and
            saved["observedAt"] >= submitted_at):
        return saved
    path = local_codex_rollout(target)
    if path is None:
        return None
    try:
        observed = _exact_delegated_input(path, target, digest, submitted_at)
    except OSError:
        return None
    if observed is None:
        return None
    receipt = {"threadId": target, "promptDigest": digest,
               "submittedAt": submitted_at, **observed}
    try:
        with DELEGATION_RECEIPT_LOCK:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            bridge.atomic_json(receipt_path, receipt)
    except OSError:
        pass  # The complete native input above is still evidence for this read.
    return receipt


def operation_delivery_snapshot(operation_id: int) -> dict:
    """Report separately the native queue acknowledgement and observed work.

    A successful ``codex queue`` only stores text.  The response observer
    binds the exact operation to native user/final items; a wake URI alone is
    not delivery evidence.  This function never resends or wakes a thread.
    """
    result = bridge.operation_result(operation_id)
    if result is None:
        return {"state": "queued", "deliveryStage": "pending"}
    snapshot = dict(result)
    if snapshot.get("scopeBlocked"):
        snapshot["deliveryStage"] = "failed"
        return snapshot
    gateway = _native_gateway_checkpoint(operation_id)
    if gateway is not None:
        receipt = gateway["receipt"]
        target = gateway["target"]
        identifiers = {key: receipt.get(key) for key in ("turnId", "clientUserMessageId")}
        exact = (gateway["state"] in {"effect_confirmed", "replying", "replied", "acked", "uncertain"}
                 and receipt.get("status") == "sent" and receipt.get("threadId") == target
                 and snapshot.get("scopeThreadId") == target
                 and all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value)
                         for value in identifiers.values()))
        snapshot["deliveryStage"] = "received" if exact else "uncertain"
        snapshot["deliveryReason"] = "exact_gateway_user_ack" if exact else "native_gateway_receipt_unconfirmed"
        if exact:
            snapshot.update(nativeTurnId=identifiers["turnId"],
                            clientUserMessageId=identifiers["clientUserMessageId"])
        # An engine ACK proves receipt of this input, never completion of its
        # resulting work. The question resolver separately observes its answer.
        return snapshot
    if snapshot.get("ok") is False:
        target = _uncertain_native_target(operation_id)
        if snapshot.get("dispatch_status") == "uncertain" or target:
            snapshot["deliveryStage"] = "uncertain"
            snapshot["deliveryReason"] = "native_effect_unconfirmed"
            if target:
                observed = _uncertain_decision_delegation(operation_id, target)
                if observed:
                    snapshot["deliveryStage"] = "received_by_active_turn"
                    snapshot["deliveryReason"] = "exact_native_delegation"
                    snapshot["nativeInputItemId"] = observed["itemId"]
        else:
            snapshot["deliveryStage"] = "failed"
        return snapshot
    if snapshot.get("ok") is not True:
        snapshot["deliveryStage"] = "pending"
        return snapshot
    # Worker results arrive after the action. Fast dispatch reports success
    # as soon as the native queue stores the message, before the chat sees it.
    if snapshot.get("dispatch_status") not in ("accepted", "accepted_wake_failed"):
        snapshot["deliveryStage"] = "completed"
        snapshot["deliveryReason"] = "worker_result"
        return snapshot
    from fast_dispatch import dispatch_receipt, native_queue_item_present

    try:
        dispatch = dispatch_receipt(bridge.APP_DIR / "fast_dispatch.sqlite", operation_id)
    except (OSError, sqlite3.Error, ValueError):
        dispatch = None
    if not dispatch or dispatch.get("threadId") != snapshot.get("scopeThreadId"):
        snapshot["deliveryStage"] = "uncertain"
        snapshot["deliveryReason"] = "exact_dispatch_receipt_missing"
        return snapshot
    queue_id, client_id = dispatch.get("queueItemId"), dispatch.get("clientUserMessageId")
    try:
        from response_watch import ledger_status, observer_ledger_path
        receipt = ledger_status(observer_ledger_path(bridge), operation_id).get("operation")
    except (OSError, sqlite3.Error, ValueError):
        receipt = None
    if receipt and receipt.get("state") in ("uncertain", "failed"):
        snapshot["deliveryStage"] = "uncertain"
        snapshot["deliveryReason"] = "native_observer_unconfirmed"
        return snapshot
    # Legacy hash/time matches do not identify the exact queued item.
    if client_id and receipt and receipt.get("userItemId") and receipt.get("finalItemId"):
        snapshot["deliveryStage"] = "completed"
        snapshot["deliveryReason"] = "exact_native_final"
    elif client_id and receipt and receipt.get("userItemId"):
        snapshot["deliveryStage"] = "received"
        snapshot["deliveryReason"] = "exact_native_user"
    else:
        try:
            present = native_queue_item_present(queue_id, dispatch["threadId"]) if queue_id else None
        except (OSError, sqlite3.Error, ValueError):
            present = None
        snapshot["deliveryStage"] = "queued" if present is True else "uncertain"
        snapshot["deliveryReason"] = ("exact_native_queue" if present is True else
                                      "native_queue_missing" if present is False else "native_queue_unverified")
    return snapshot


def with_accepted_messages(thread_id: str, history: list, *, match_history: list | None = None,
                           history_start=None) -> list:
    """Keep accepted owner messages visible until the Desktop history contains them."""
    if not REQUEST_INDEX.exists():
        return history
    with REQUEST_LOCK:
        requests = bridge.read_json(REQUEST_INDEX)
    now = time.time()
    accepted = sorted(
        (row for row in requests.values() if row.get("kind") == "followup"
         and row.get("target") == thread_id and isinstance(row.get("message"), str)
         and now - row.get("at", 0) < 7 * 86400),
        key=lambda row: (row["at"], row["operationId"]),
    )[-50:]
    if not accepted:
        return history
    results = bridge.read_json(bridge.RESULTS_PATH) if bridge.RESULTS_PATH.exists() else {}
    user_items = [(item.get("text"), message_timestamp(item.get("createdAt")))
                  for item in (history if match_history is None else match_history)
                  if item.get("role") in ("user", "human")]
    used = set()
    extra = []
    for row in accepted:
        at = row["at"]
        result = results.get(str(row["operationId"]))
        if history_start is not None and result and result.get("ok") is True and at < history_start - 10:
            continue
        match = next((index for index, (text, created) in enumerate(user_items)
                      if result and result.get("ok") is True and index not in used
                      and text == row["message"] and created is not None and created >= at - 10), None)
        if match is not None:
            used.add(match)
            continue
        # An accepted queue item is not a native UserMessage yet.  The panel
        # must not label it sent until the exact user item is in the history.
        delivery = ("uncertain" if result and result.get("dispatch_status") == "uncertain" else
                    "failed" if result and result.get("ok") is False else
                    "queued" if result and result.get("ok") is True else "pending")
        extra.append({"id": "local-" + str(row["operationId"]), "operationId": str(row["operationId"]),
                      "role": "user", "text": row["message"], "createdAt": at,
                      "attachments": row.get("attachments", []), "queued": True, "delivery": delivery})
    return [*history, *extra]


def normal_project(item: dict) -> dict:
    result = dict(item)
    result["id"] = item.get("projectId")
    result["kind"] = item.get("projectKind", "Codex")
    result["name"] = item.get("label") or item.get("name") or item.get("projectId")
    return result


def import_project_file(path_text: str, thread_id: str) -> dict:
    if not isinstance(path_text, str) or not isinstance(thread_id, str) or len(path_text) > 1000:
        raise ValueError("Укажите путь к файлу и чат")
    thread = next((x for x in thread_rows() if x.get("id") == thread_id), None)
    if not thread or "chatgpt" in str(thread.get("kind", "")).casefold():
        raise ValueError("Файл можно получить только из локального чата Codex")
    project = next((p for p in project_rows() if p.get("projectId") == thread.get("projectId")), None)
    root_text = (project or {}).get("path") or thread.get("cwd")
    if not root_text:
        raise ValueError("Для этого чата рабочий каталог не определён")
    root = Path(root_text).resolve(strict=True)
    source = Path(path_text).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in ALLOWED or source.stat().st_size > MAX_UPLOAD:
        raise ValueError("Файл отсутствует, имеет неподдерживаемый тип или больше 20 МБ")
    if root not in source.parents:
        raise ValueError("Разрешены только файлы внутри выбранного проекта")
    return store_file(source.name, source.read_bytes())


def artifact_thread(thread_id: str) -> dict:
    thread = next((row for row in thread_rows() if row.get("id") == thread_id), None)
    if thread is None or not bridge.thread_in_scope(thread_id):
        raise ValueError("Чат не найден или исключён из панели")
    return thread


def thread_artifacts(thread_id: str) -> dict:
    """Discover every server-derived artifact from the thread's full history."""
    thread = artifact_thread(thread_id)
    base_dir = thread.get("cwd")
    if not base_dir and thread.get("projectId"):
        project = next((row for row in project_rows() if row.get("projectId") == thread.get("projectId")), None)
        base_dir = (project or {}).get("path")
    rollout = local_codex_rollout(thread_id)
    if rollout is not None:
        return artifact_results.list_thread_artifacts(thread_id, rollout, base_dir=base_dir,
                                                      snapshot_dir=ARTIFACT_FILES)
    try:
        from artifact_native_loader import load_native_artifacts
        native = load_native_artifacts(thread_id, host_id=thread.get("hostId"))
    except Exception:
        return {"artifacts": [], "complete": False, "reason": "native_history_unavailable"}
    return artifact_results.list_native_artifacts(thread_id, native, base_dir=base_dir,
                                                  snapshot_dir=ARTIFACT_FILES)


def artifact_download_ticket(thread_id: str, artifact_id: str, payload: dict) -> dict:
    if payload:
        raise ValueError("Для скачивания параметры не требуются")
    # Revalidate owner-visible scope and recompute the server-derived artifact;
    # the browser can never supply a filesystem path.
    artifact_thread(thread_id)
    found = artifact_results.find_artifact(thread_artifacts(thread_id), artifact_id)
    if found is None:
        raise ValueError("Артефакт не найден. Обновите список файлов")
    return artifact_results.issue_download_ticket(found, snapshot_dir=ARTIFACT_FILES)


def validated_settings(payload: dict, *, chatgpt: bool) -> dict:
    model, thinking = payload.get("model") or None, payload.get("thinking") or None
    if chatgpt and (model or thinking):
        raise ValueError("Для ChatGPT выбор модели недоступен")
    if model and model not in MODEL_EFFORTS:
        raise ValueError("Модель не поддерживается")
    if thinking and (not model or thinking not in MODEL_EFFORTS[model]):
        raise ValueError("Интенсивность не подходит для модели")
    return {"model": model, "thinking": thinking}


def validated_attachments(payload: dict, *, chatgpt: bool) -> list:
    ids = payload.get("attachmentIds") or []
    if not isinstance(ids, list) or len(ids) > 10 or any(not isinstance(x, str) for x in ids):
        raise ValueError("Некорректный список вложений")
    if chatgpt and ids:
        raise ValueError("Вложения в ChatGPT Work через Desktop пока недоступны")
    files = []
    for file_id in ids:
        row = get_file(file_id)
        if row is None:
            raise ValueError("Вложение не найдено")
        entry, path = row
        files.append({"id": file_id, "name": entry["name"], "path": str(path), "mimeType": entry["mimeType"]})
    return files


def queue_once(payload: dict, kind: str, message: str, target: str, settings: dict, attachments: list) -> int:
    request_id = payload.get("operationId")
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", request_id)):
        raise ValueError("Некорректный идентификатор запроса")
    with REQUEST_LOCK:
        rows = bridge.read_json(REQUEST_INDEX) if REQUEST_INDEX.exists() else {}
        if request_id and request_id in rows:
            previous = rows[request_id]
            if previous.get("target") != target or previous.get("kind") != kind or previous.get("digest") != hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest():
                raise ValueError("Идентификатор уже использован другим запросом")
            operation_id = previous["operationId"]
        elif request_id:
            # Reserve without publishing to worker/CLI. The mapping must exist
            # before an external effect, including a successful fast dispatch.
            operation_id = bridge.prepare_web(kind, message, target, settings, attachments)
            rows[request_id] = {"operationId": operation_id, "target": target, "kind": kind, "digest": hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest(), "at": int(time.time())}
            if kind == "followup":
                rows[request_id]["message"] = message
                rows[request_id]["attachments"] = [{key: item.get(key) for key in ("id", "name", "mimeType")} for item in attachments]
            if len(rows) > 2000:
                rows = dict(sorted(rows.items(), key=lambda row: row[1]["at"])[-2000:])
            bridge.atomic_json(REQUEST_INDEX, rows)
        else:
            return bridge.enqueue_web(kind, message, target, settings, attachments)
    # Existing IDs resume the same staging. Finished/ACKed reservations are a
    # no-op here; they never allocate a new pending command.
    return bridge.finish_web(operation_id)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def respond(self, code: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        etag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"' if self.command == "GET" and code == 200 else None
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        compressed = len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "").lower()
        wire_body = gzip.compress(body, compresslevel=5, mtime=0) if compressed else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(wire_body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(wire_body)

    def authenticated(self) -> bool:
        try:
            valid = verify_init_data(self.headers.get("X-Telegram-Init-Data", ""))
        except Exception:
            valid = False
        if not valid:
            self.respond(401, {"error": "Откройте панель через Telegram"})
        return valid

    def read_body(self) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Некорректная длина запроса")
        if not 0 < size <= MAX_UPLOAD + 20000:
            raise ValueError("Запрос слишком большой")
        return self.rfile.read(size)

    def read_json(self) -> dict:
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("Ожидался JSON")
        obj = json.loads(self.read_body())
        if not isinstance(obj, dict):
            raise ValueError("Ожидался объект")
        return obj

    def read_multipart(self) -> tuple[str, bytes]:
        content_type, params = cgi.parse_header(self.headers.get("Content-Type", ""))
        if content_type != "multipart/form-data" or not params.get("boundary"):
            raise ValueError("Ожидался файл")
        body = self.read_body()
        marker = b"--" + params["boundary"].encode()
        for part in body.split(marker):
            if b"\r\n\r\n" not in part:
                continue
            head, content = part.split(b"\r\n\r\n", 1)
            if b'name="file"' not in head:
                continue
            match = re.search(br'filename="([^"\r\n]{1,200})"', head)
            if not match:
                continue
            name = match.group(1).decode("utf-8", "replace")
            return name, content.removesuffix(b"\r\n")
        raise ValueError("Файл отсутствует")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/_bridge_health":
            # Public liveness only: no owner, status, task or secret access.
            match = re.fullmatch(r"nonce=([a-f0-9]{32})", urlsplit(self.path).query)
            if not match:
                return self.respond(400, {"ok": False})
            return self.respond(200, {"ok": True, "service": "telegram-desktop", "nonce": match.group(1)})
        download = re.fullmatch(r"/api/artifact-download/([A-Za-z0-9_-]{40,64})", path)
        if download:
            return self.artifact_download(download.group(1))
        if path in ("/api/bridge-challenge", "/api/bridge-deliver"):
            return self.respond(405, {"ok": False, "error": "method_not_allowed"})
        if not path.startswith("/api/"):
            return self.static(path)
        if not self.authenticated():
            return
        try:
            if path.startswith('/api/kanban/'):
                from kanban_proxy import get_proxy, KanbanError
                try:
                    query_pairs = parse_qsl(urlsplit(self.path).query, keep_blank_values=True)
                    if path in ('/api/kanban/state', '/api/kanban/export'):
                        if query_pairs:
                            return self.respond(400, {'error': 'Параметры запроса не поддерживаются.'})
                        proxy = get_proxy()
                        return self.respond(200, proxy.state() if path.endswith('/state') else proxy.export())
                    if path in ('/api/kanban/file', '/api/kanban/source'):
                        if len(query_pairs) != 1 or query_pairs[0][0] != 'path':
                            return self.respond(400, {'error': 'Укажите один относительный путь path.'})
                        return self.respond(200, get_proxy().document(path.rsplit('/', 1)[1], query_pairs[0][1]))
                    return self.respond(404, {'error': 'Маршрут доски не найден.'})
                except KanbanError as exc:
                    return self.respond(exc.status, exc.payload)
            coverage = re.fullmatch(r"/api/worker/coverage/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", path)
            if coverage:
                # Diagnostic AgentMessage observer status, not terminal-state coverage.
                return self.respond(200, bridge.activity_coverage(coverage.group(1)))
            if path == "/api/threads":
                return self.respond(200, {"threads": thread_rows()})
            if path == "/api/projects":
                return self.respond(200, {"projects": [normal_project(p) for p in project_rows()]})
            if path == "/api/archived":
                return self.respond(200, {"threads": archived_rows()})
            if path == "/api/usage":
                return self.respond(200, get_usage_service().snapshot())
            if path == "/api/decisions":
                from decision_service import list_for_thread
                query = dict(parse_qsl(urlsplit(self.path).query))
                thread_id = query.get("thread", "")
                if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", thread_id):
                    return self.respond(400, {"error": "Укажите текущий чат"})
                return self.respond(200, list_for_thread(bridge, thread_id, operation_delivery_snapshot))
            if path == "/api/study-status":
                from study_status import current
                try:
                    return self.respond(200, current())
                except Exception:
                    return self.respond(200, {"available": False, "error": "Локальная панель сейчас недоступна"})
            progress = re.fullmatch(r"/api/threads/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/progress", path)
            if progress:
                thread_id = progress.group(1)
                try:
                    bridge.require_thread_scope(thread_id)
                except ValueError:
                    return self.respond(404, {"available": False, "error": "Чат недоступен"})
                try:
                    return self.respond(200, task_progress_snapshot(thread_id))
                except Exception:
                    return self.respond(200, {"available": False, "error": "Прогресс задачи сейчас недоступен"})
            artifact_list = re.fullmatch(r"/api/threads/([A-Za-z0-9-]{1,100})/artifacts", path)
            if artifact_list:
                return self.respond(200, thread_artifacts(artifact_list.group(1)))
            match = re.fullmatch(r"/api/threads/([A-Za-z0-9-]{1,100})", path)
            if match:
                thread_id = match.group(1)
                row = next((x for x in thread_rows() if x.get("id") == thread_id), None)
                if row is None:
                    return self.respond(404, {"error": "Чат не найден"})
                if "chatgpt" not in str(row.get("kind", "")).casefold():
                    before = dict(parse_qsl(urlsplit(self.path).query)).get("before")
                    page = local_history_page(thread_id, before)
                    if page is not None:
                        if before is None:
                            first_at = message_timestamp(page["messages"][0].get("createdAt")) if page["messages"] else None
                            page["messages"] = with_accepted_messages(thread_id, page["messages"], history_start=first_at)
                        return self.respond(200, {"thread": row, **page})
                detail_path = bridge.THREAD_DETAILS / (thread_id + ".bin")
                detail = bridge.read_json(detail_path).get("detail", {}) if detail_path.exists() else {}
                if not detail or (detail_path.exists() and time.time() - detail_path.stat().st_mtime > 180):
                    with VIEW_LOCK:
                        if time.monotonic() - VIEW_AT.get(thread_id, 0) > 180:
                            bridge.enqueue_web("view", "", thread_id)
                            VIEW_AT[thread_id] = time.monotonic()
                return self.respond(200, {"thread": row, "messages": with_accepted_messages(thread_id, detail.get("messages") or []), "nextCursor": detail.get("nextCursor")})
            match = re.fullmatch(r"/api/files/([a-f0-9]{32})", path)
            if match:
                found = get_file(match.group(1))
                if found is None:
                    return self.respond(404, {"error": "Файл не найден"})
                entry, file_path = found
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", entry["mimeType"])
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(entry["name"]))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return self.wfile.write(data)
            match = re.fullmatch(r"/api/operations/(\d{1,20})", path)
            if match:
                return self.respond(200, operation_delivery_snapshot(int(match.group(1))))
            return self.respond(404, {"error": "Не найдено"})
        except Exception:
            return self.respond(500, {"error": "Ошибка локальной панели"})

    def do_HEAD(self):
        path = urlsplit(self.path).path
        download = re.fullmatch(r"/api/artifact-download/([A-Za-z0-9_-]{40,64})", path)
        if download:
            return self.artifact_download(download.group(1))
        self.send_error(501)

    def artifact_download(self, token: str):
        try:
            download = artifact_results.open_download_ticket(token, self.headers.get("Range"))
        except artifact_results.RangeError as exc:
            self.send_response(416)
            if exc.total is not None:
                self.send_header("Content-Range", "bytes */" + str(exc.total))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "https://web.telegram.org")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        except artifact_results.TicketError as exc:
            code = 410 if str(exc) in {"ticket_expired", "artifact_missing", "artifact_changed"} else 404
            if self.command == "HEAD":
                return self.send_error(code)
            return self.respond(code, {"error": "Ссылка на файл недействительна или устарела"})
        try:
            name = Path(download.name)
            ascii_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name.stem).strip("._") or "download"
            ascii_suffix = re.sub(r"[^A-Za-z0-9._-]+", "_", name.suffix)
            disposition = (f'attachment; filename="{ascii_stem}{ascii_suffix}"; '
                           "filename*=UTF-8''" + quote(download.name, safe=""))
            self.send_response(download.status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(download.length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "https://web.telegram.org")
            if download.status == 206:
                self.send_header("Content-Range", f"bytes {download.start}-{download.end}/{download.total}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command == "HEAD":
                return
            remaining = download.length
            while remaining:
                chunk = download.stream.read(min(1_048_576, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        finally:
            download.close()

    def static(self, path: str):
        files = {"/": "index.html", "/index.html": "index.html", "/styles.css": "styles.css", "/app.js": "app.js", "/marble.svg": "marble.svg", "/task-progress.js": "task-progress.js", "/task-progress.css": "task-progress.css", "/kanban.js": "kanban.js", "/kanban.css": "kanban.css"}
        name = files.get(path)
        if not name:
            return self.respond(404, {"error": "Не найдено"})
        source = STATIC / name
        data = source.read_bytes()
        encoding = ""
        accepted = self.headers.get("Accept-Encoding", "").lower()
        brotli = STATIC / (name + ".br")
        if "br" in accepted and brotli.is_file() and brotli.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            data = brotli.read_bytes()
            encoding = "br"
        elif "gzip" in accepted and len(data) > 1024:
            data = gzip.compress(data, compresslevel=9, mtime=0)
            encoding = "gzip"
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Security-Policy", "default-src 'self' https://telegram.org; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; base-uri 'none'; frame-ancestors https://web.telegram.org")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path in ("/api/bridge-challenge", "/api/bridge-deliver"):
            return self.cloud_push(path)
        if not self.authenticated():
            return
        try:
            if path.startswith('/api/kanban/'):
                if path != '/api/kanban/update':
                    return self.respond(404, {'error': 'Маршрут доски не найден.'})
                if urlsplit(self.path).query or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Encoding', 'identity') != 'identity':
                    return self.respond(400, {'error': 'Неподдерживаемый формат записи доски.'})
                length = self.headers.get('Content-Length', '')
                if not re.fullmatch(r'[0-9]+', length) or not 0 < int(length) <= 100000:
                    return self.respond(413, {'error': 'Запись доски слишком велика или пуста.'})
                from kanban_proxy import get_proxy, KanbanError
                try:
                    return self.respond(200, get_proxy().update(self.read_json()))
                except KanbanError as exc:
                    return self.respond(exc.status, exc.payload)
            decision_answer = re.fullmatch(r"/api/decisions/([a-f0-9]{20})/answer", path)
            if decision_answer:
                from decision_service import answer
                payload = self.read_json()
                if set(payload) not in ({"choiceId"}, {"text"}):
                    raise ValueError("Выберите один вариант или введите свой ответ")
                result = answer(bridge, decision_answer.group(1),
                                choice_id=payload.get("choiceId"), text=payload.get("text"),
                                delivery_snapshot=operation_delivery_snapshot)
                return self.respond(202, result)
            if path == "/api/usage/refresh":
                self.read_json()
                return self.respond(200, get_usage_service().refresh())
            history_match = re.fullmatch(r"/api/threads/([A-Za-z0-9-]{1,100})/history", path)
            if history_match:
                payload = self.read_json()
                target = history_match.group(1)
                thread = next((x for x in thread_rows() if x.get("id") == target), None)
                if thread is None:
                    raise ValueError("Чат не найден")
                detail_path = bridge.THREAD_DETAILS / (target + ".bin")
                detail = bridge.read_json(detail_path).get("detail", {}) if detail_path.exists() else {}
                cursor = payload.get("cursor")
                if not isinstance(cursor, str) or not cursor or len(cursor) > 3000 or cursor != detail.get("nextCursor"):
                    raise ValueError("Страница истории устарела. Обновите чат")
                operation_id = queue_once(payload, "view_more", cursor, target, {}, [])
                return self.respond(202, {"accepted": True, "operationId": str(operation_id)})
            action_match = re.fullmatch(r"/api/threads/([A-Za-z0-9-]{1,100})/actions", path)
            if action_match:
                payload = self.read_json()
                target = action_match.group(1)
                action = payload.get("action")
                source_rows = archived_rows() if action == "restore" else thread_rows()
                thread = next((x for x in source_rows if x.get("id") == target), None)
                if thread is None:
                    raise ValueError("Чат не найден. Обновите панель")
                if action not in {"rename", "pin", "unpin", "archive", "restore"}:
                    raise ValueError("Неизвестное действие с чатом")
                if action == "restore" and "chatgpt" in str(thread.get("kind", "")).casefold():
                    raise ValueError("Восстановление ChatGPT-чата через Desktop недоступно")
                value = payload.get("value", "")
                if action == "rename":
                    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
                        raise ValueError("Название должно содержать от 1 до 100 символов")
                    value = value.strip()
                elif value:
                    raise ValueError("Для этого действия значение не требуется")
                operation_id = queue_once(payload, "thread_" + action, value, target, {}, [])
                return self.respond(202, {"accepted": True, "operationId": str(operation_id)})
            artifact_ticket = re.fullmatch(r"/api/threads/([A-Za-z0-9-]{1,100})/artifacts/([0-9a-f]{32})/download-ticket", path)
            if artifact_ticket:
                payload = self.read_json()
                result = artifact_download_ticket(artifact_ticket.group(1), artifact_ticket.group(2), payload)
                return self.respond(200, result)
            if path == "/api/uploads":
                name, data = self.read_multipart()
                return self.respond(200, store_file(name, data))
            if path == "/api/files/import":
                payload = self.read_json()
                return self.respond(200, import_project_file(payload.get("path"), payload.get("threadId")))
            if path == "/api/voice/transcribe":
                name, data = self.read_multipart()
                if len(data) > MAX_UPLOAD:
                    raise ValueError("Запись слишком большая")
                import tempfile
                with tempfile.TemporaryDirectory(prefix="miniapp_voice_") as folder:
                    ext = Path(name).suffix.lower()
                    if ext not in {".webm", ".ogg", ".mp3", ".m4a", ".wav"}:
                        raise ValueError("Формат аудио не поддерживается")
                    audio = Path(folder) / ("voice" + ext)
                    audio.write_bytes(data)
                    result = subprocess.run([str(bridge.ASR_PYTHON), str(Path(__file__).with_name("transcribe_voice.py")), str(bridge.ASR_MODEL), str(audio)], capture_output=True, timeout=240, check=True, encoding="utf-8", errors="replace")
                    transcript = json.loads(result.stdout).get("text", "").strip()
                return self.respond(200, {"transcript": transcript[:4000]})
            if path == "/api/tasks":
                payload = self.read_json()
                project_id = payload.get("projectId")
                project = next((p for p in project_rows() if p.get("projectId") == project_id), None)
                if project is None:
                    raise ValueError("Проект не найден. Обновите панель")
                chatgpt = "chatgpt" in str(project.get("projectKind", "")).casefold()
                kind = "new_task"
                target = project_id
            else:
                match = re.fullmatch(r"/api/threads/([A-Za-z0-9-]{1,100})/messages", path)
                if not match:
                    return self.respond(404, {"error": "Не найдено"})
                payload = self.read_json()
                target = match.group(1)
                thread = next((x for x in thread_rows() if x.get("id") == target), None)
                if thread is None:
                    raise ValueError("Чат не найден. Обновите панель")
                chatgpt = "chatgpt" in str(thread.get("kind", "")).casefold()
                kind = "followup"
            message = payload.get("text", "")
            if not isinstance(message, str) or len(message) > 4000:
                raise ValueError("Текст слишком длинный")
            attachments = validated_attachments(payload, chatgpt=chatgpt)
            if not message.strip() and not attachments:
                raise ValueError("Введите текст или добавьте файл")
            settings = validated_settings(payload, chatgpt=chatgpt)
            operation_id = queue_once(payload, kind, message, target, settings, attachments)
            return self.respond(202, {"accepted": True, "operationId": str(operation_id)})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.respond(400, {"error": str(exc)})
        except Exception:
            return self.respond(500, {"error": "Ошибка локальной панели"})

    def cloud_push(self, path):
        """Bound unauthenticated input; never reuse Telegram Mini App auth."""
        self.close_connection = True
        if urlsplit(self.path).query or self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding", "identity") != "identity":
            return self.respond(400, {"ok": False, "error": "invalid_request"})
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            return self.respond(415, {"ok": False, "error": "unsupported_content_type"})
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]+", length) or not 0 < int(length) <= 131072:
            return self.respond(413, {"ok": False, "error": "request_too_large"})
        try:
            self.connection.settimeout(5)
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                return self.respond(400, {"ok": False, "error": "invalid_request"})
            status, result = cloud_push_service().handle(path, raw)
        except Exception:
            status, result = 503, {"ok": False, "error": "push_unavailable"}
        return self.respond(status, result)


def serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="TelegramMiniApp")
    thread.start()
    return server


DESKTOP_MENU_LOCK = threading.Lock()
_LAST_DESKTOP_MENU = None


def sync_desktop_menu(value: dict) -> bool:
    """Publish only the latest probed URL; never send a message or reset the button.

    Cloud delivery owns /panels and slash commands. The tunnel is the only
    owner of the paired chat's permanent Desktop entry, in either transport.
    Failed idempotent setters can be retried at the next successful probe;
    they must not tear down a healthy tunnel or consume its new URL.
    """
    global _LAST_DESKTOP_MENU
    from desktop_transport import miniapp_status
    try:
        status = miniapp_status(lambda: value)
        if not status["healthy"]:
            return False
        owner = bridge.state().get("owner_chat_id")
        if type(owner) is not int or not 0 < owner <= 9007199254740991:
            return False
        desired = (owner, status["url"])
        with DESKTOP_MENU_LOCK:
            if desired == _LAST_DESKTOP_MENU:
                return False
            if bridge.api("setChatMenuButton", {"chat_id": owner, "menu_button": {
                    "type": "web_app", "text": "Desktop", "web_app": {"url": status["url"]}}}, timeout=5) is not True:
                return False
            _LAST_DESKTOP_MENU = desired
            return True
    except Exception:
        return False


def write_tunnel_status(value: dict) -> None:
    bridge.atomic_json(bridge.APP_DIR / "miniapp_status.bin", value)
    # Metadata timestamp refreshes do not trigger network calls. The client
    # wakes only if the healthy URL actually changed.
    try:
        from desktop_control import notify_panel_changed
        notify_panel_changed()
    except Exception:
        pass
    # URL registration must be signalled before Telegram's optional UI setter.
    # Its bounded request may fail while the healthy tunnel remains usable.
    sync_desktop_menu(value)
    # A healthy-URL transition is a recovery event, including restoration after
    # network loss. Routine timestamp refreshes do not trigger a queue fetch.
    try:
        from cloud_recovery import notify_panel_changed as notify_cloud_recovery
        notify_cloud_recovery()
    except Exception:
        pass


class TunnelProbeNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("tunnel_probe_redirect_rejected")


def probe_tunnel(url: str, *, open_request=None) -> bool:
    """One existing-cycle probe must reach the server with a fresh nonce.

    Static HTML, a cached response, a provider error page and a redirect are
    not liveness. The public endpoint echoes no owner data or credentials.
    """
    from desktop_transport import PANEL_PATTERN
    if not isinstance(url, str) or PANEL_PATTERN.fullmatch(url) is None:
        return False
    nonce = secrets.token_hex(16)
    request = urllib.request.Request(url.rstrip("/") + "/_bridge_health?nonce=" + nonce,
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
                 "Accept": "application/json", "Cache-Control": "no-cache, no-store"})
    try:
        transport = open_request or urllib.request.build_opener(TunnelProbeNoRedirect()).open
        with transport(request, timeout=12) as response:
            if response.status != 200 or response.geturl() != request.full_url or not response.headers.get("Content-Type", "").lower().startswith("application/json"):
                return False
            body = response.read(2049)
            if len(body) > 2048:
                return False
            return json.loads(body) == {"ok": True, "service": "telegram-desktop", "nonce": nonce}
    except Exception:
        return False


def tunnel_lifecycle_event(reason: str, *, failures: int = 0, outage_seconds: float = 0) -> None:
    """Bounded local diagnostics only: never persist URLs, auth or chat identifiers."""
    if reason not in {"ssh_started", "probe_unhealthy", "probe_recovered", "sustained_probe_outage",
                      "ssh_exited", "supervisor_error"}:
        return
    entry = {"at": datetime.now().astimezone().isoformat(), "reason": reason,
             "failures": max(0, int(failures)), "outage_seconds": round(max(0, outage_seconds), 3)}
    try:
        path = bridge.APP_DIR / "tunnel_lifecycle.jsonl"
        with TUNNEL_LIFECYCLE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 131072:
                path.replace(path.with_suffix(".previous.jsonl"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def supervise_localhostrun() -> None:
    ssh = shutil.which("ssh") or (str(Path("C:/Windows/System32/OpenSSH/ssh.exe")) if Path("C:/Windows/System32/OpenSSH/ssh.exe").exists() else None)
    if not ssh:
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    while True:
        process = None
        current_url = ""
        last_candidate = ""
        try:
            process = subprocess.Popen(
                [ssh, "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
                 "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=2",
                 "-o", "ExitOnForwardFailure=yes", "-R", f"80:{HOST}:{PORT}",
                 "nokey@localhost.run", "--", "--output", "json"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                errors="replace", creationflags=flags,
            )
            tunnel_lifecycle_event("ssh_started")
            urls = queue.Queue()

            def collect_urls() -> None:
                for line in process.stdout:
                    match = re.search(r"https://[a-z0-9.-]+\.lhr\.life", line)
                    if match:
                        urls.put(match.group(0))

            threading.Thread(target=collect_urls, daemon=True).start()
            failures = 0
            failure_started = None
            while process.poll() is None:
                try:
                    candidate = urls.get(timeout=15)
                except queue.Empty:
                    candidate = last_candidate
                if not candidate:
                    continue
                # Retain the advertised address even if its first probe fails.
                # SSH may print it only once; an empty queue must retry it.
                last_candidate = candidate
                healthy = probe_tunnel(candidate)
                checked_at = time.monotonic()
                if healthy:
                    if failures:
                        tunnel_lifecycle_event("probe_recovered", failures=failures,
                                               outage_seconds=checked_at - failure_started)
                    failures = 0
                    failure_started = None
                    # Freshness records the last successful tunnel probe, not
                    # merely the date when this URL was first allocated.
                    write_tunnel_status({
                        "url": candidate, "at": int(time.time()),
                        "provider": "localhost.run", "healthy": True,
                    })
                    current_url = candidate
                else:
                    # Revoke health immediately, but preserve the open WebView's
                    # origin while a live SSH tunnel recovers from transient 5xx.
                    # SSH keepalive independently detects an actual dead session.
                    write_tunnel_status({
                        "url": candidate, "at": int(time.time()),
                        "provider": "localhost.run", "healthy": False,
                    })
                    failures += 1
                    if failure_started is None:
                        failure_started = checked_at
                        tunnel_lifecycle_event("probe_unhealthy", failures=failures)
                    outage_seconds = checked_at - failure_started
                    if failures >= TUNNEL_FAILURE_LIMIT and outage_seconds >= TUNNEL_OUTAGE_GRACE_SECONDS:
                        tunnel_lifecycle_event("sustained_probe_outage", failures=failures,
                                               outage_seconds=outage_seconds)
                        break
            if process.poll() is not None:
                tunnel_lifecycle_event("ssh_exited")
        except Exception:
            tunnel_lifecycle_event("supervisor_error")
        finally:
            if process and process.poll() is None:
                process.terminate()
            try:
                write_tunnel_status({
                    "url": last_candidate or current_url, "at": int(time.time()),
                    "provider": "localhost.run", "healthy": False,
                })
            except Exception:
                pass
        time.sleep(5)


def supervise_pinggy() -> None:
    cli = Path(__file__).parent / "vendor" / "pinggy" / "node_modules" / "pinggy" / "dist" / "index.cjs"
    node = shutil.which("node") or (str(Path("C:/Program Files/nodejs/node.exe")) if Path("C:/Program Files/nodejs/node.exe").exists() else None)
    if not cli.exists() or not node:
        return supervise_cloudflare()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    current_url = ""
    failures = 0
    while True:
        tunnel_id = ""
        url = ""
        try:
            result = subprocess.run([node, str(cli), "ps"], capture_output=True, text=True, timeout=12, creationflags=flags)
            for line in result.stdout.splitlines():
                columns = line.split()
                if len(columns) >= 5 and columns[2] == "running" and columns[3] == f"{HOST}:{PORT}" and columns[4].startswith("https://"):
                    tunnel_id, url = columns[0], columns[4]
                    break
            if not url:
                started = subprocess.run([node, str(cli), "-l", f"{HOST}:{PORT}", "--b"], capture_output=True, text=True, timeout=35, creationflags=flags)
                match = re.search(r"https://[a-z0-9.-]+", started.stdout)
                url = match.group(0) if started.returncode == 0 and match else ""
            healthy = False
            if url:
                try:
                    with urllib.request.urlopen(url, timeout=12) as response:
                        healthy = response.status == 200 and b"<title" in response.read(20000)
                except Exception:
                    pass
            if healthy:
                failures = 0
                write_tunnel_status({"url": url, "at": int(time.time()), "provider": "pinggy", "healthy": True})
                current_url = url
            else:
                failures += 1
                write_tunnel_status({"url": current_url, "at": int(time.time()), "provider": "pinggy", "healthy": False})
                current_url = ""
                if failures >= 3 and tunnel_id:
                    subprocess.run([node, str(cli), "stop", tunnel_id], capture_output=True, text=True, timeout=12, creationflags=flags)
                    failures = 0
        except Exception:
            pass
        time.sleep(20)


def supervise_tunnel() -> None:
    if config.settings().get("tunnel_provider") == "localhost.run":
        supervise_localhostrun()


def supervise_cloudflare() -> None:
    executable = Path.home() / ".cache" / "CodexTelegramDesktop" / "cloudflared.exe"
    if not executable.exists():
        return
    while True:
        process = None
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen([str(executable), "tunnel", "--url", f"http://{HOST}:{PORT}", "--no-autoupdate"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=flags)
            url = None
            for line in process.stderr:
                if "Registered tunnel connection" in line or " ERR " in line or "Retrying connection" in line:
                    log = bridge.APP_DIR / "tunnel_health.log"
                    with log.open("a", encoding="utf-8") as output:
                        output.write(line[:500])
                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if match:
                    url = match.group(0)
                if url and "Registered tunnel connection" in line:
                    try:
                        with urllib.request.urlopen(url, timeout=8) as response:
                            healthy = response.status == 200 and "Рабочий стол".encode("utf-8") in response.read(10000)
                    except Exception:
                        healthy = False
                    if healthy:
                        write_tunnel_status({"url": url, "at": int(time.time()), "provider": "cloudflare", "healthy": True})
                        url = None
        except Exception:
            pass
        finally:
            if process and process.poll() is None:
                process.terminate()
            try:
                write_tunnel_status({"url": url, "at": int(time.time()), "provider": "cloudflare", "healthy": False})
            except Exception:
                pass
        time.sleep(10)

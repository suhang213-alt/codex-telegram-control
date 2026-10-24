"""Narrow local Codex text dispatch; no model/effort overrides or attachments.

Only metadata/hash/status enter SQLite. Callers supply already authenticated
pending and native cache through the bridge's internal encrypted accessors.
"""
from __future__ import annotations
import bridge_config as config
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from desktop_wake import QueuedWakeFailure, wake_thread

MAX_CACHE_AGE = 300
QUEUE_TIMEOUT = 8
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
QUEUE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
TECHNICAL_THREADS = {config.service_thread_id()}


def fingerprint(record):
    selected = {key: record.get(key) for key in ("id", "kind", "text", "selected_thread_id", "model", "thinking", "attachments")}
    return hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def candidate(record):
    if not isinstance(record, dict) or record.get("kind") != "followup" or type(record.get("id")) is not int or record["id"] < 0:
        return False
    # Owner decisions steer the active Desktop chat through its native bridge.
    # `codex queue` can only create a subsequent turn, so never claim immediate
    # delivery by routing a decision answer through that fast path.
    if record.get("decision_answer") is True:
        return False
    if record.get("model") is not None or record.get("thinking") is not None or record.get("attachments"):
        return False
    target, text = record.get("selected_thread_id"), record.get("text")
    if (not isinstance(target, str) or UUID.fullmatch(target) is None or
            not isinstance(text, str) or not text.strip() or
            len(text.encode("utf-16-le")) // 2 > 12000):
        return False
    if target.casefold() in TECHNICAL_THREADS:
        return False
    return True


def eligible(record, rows, now):
    if not candidate(record):
        return False
    target = record["selected_thread_id"]
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == target]
    if len(matches) != 1:
        return False
    row = matches[0]
    stamp = row.get("native_verified_at")
    # The registered wake-only URI resumes in the real Desktop runtime, which
    # owns both active and cold threads. Metadata proves the exact local target;
    # status no longer decides eligibility or creates a second engine.
    verified = (row.get("native_verification") in {"codex_native_tools", "local_codex_database"}
                and str(row.get("kind")).casefold() == "codex" and row.get("hostId") == "local"
                and type(stamp) is int and 0 <= now - stamp <= MAX_CACHE_AGE)
    if not verified:
        return False
    # Prefer the existing Desktop engine's immediate turn/steer path. This is
    # discovery only, before the queue effect and its durable dispatch guard.
    # Existing queue receipts above this check remain authoritative on retry.
    from native_gateway_client import NativeGatewayClient, GatewayError
    try:
        NativeGatewayClient().discover(target)
    except GatewayError as exc:
        # Ambiguous/broken discovery must not enqueue a possible duplicate;
        # the native worker will retain the explicit diagnostic instead.
        return exc.code == "native_gateway_unavailable"
    return False


def queue_text(thread_id, text, *, on_receipt=None):
    from wake_dispatcher import codex_binary, ROOT
    result = subprocess.run([str(codex_binary()), "queue", "--thread", thread_id, "--message", text],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=QUEUE_TIMEOUT,
        encoding="utf-8", errors="strict")
    queue_id = queued_item_id(result.returncode, result.stdout, thread_id)
    client_id = native_queue_client_id(queue_id, thread_id, text) if queue_id else None
    if queue_id:
        # The dispatch ledger must hold the native IDs before Desktop is woken:
        # a fast native turn may consume the queue row before we can read it.
        if on_receipt is not None:
            on_receipt(queue_id, client_id)
        try:
            woken = wake_thread(thread_id).get("ok")
        except Exception:
            woken = False
        if not woken:
            failure = QueuedWakeFailure("desktop_wake_unconfirmed")
            failure.queue_item_id = queue_id
            failure.client_user_message_id = client_id
            raise failure
    return result.returncode, result.stdout, client_id


def queued_item_id(code, output, thread_id):
    if code != 0 or not isinstance(output, str) or len(output) > 4096:
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    match = re.fullmatch(r"Queued message ([A-Za-z0-9_-]{1,128}) for thread " + re.escape(thread_id) + r"\.", lines[0])
    return match.group(1) if match else None


def queued_confirmation(code, output, thread_id):
    return queued_item_id(code, output, thread_id) is not None


def _queue_paths(queue_db=None):
    if queue_db is not None:
        return [Path(queue_db)]
    root = config.codex_home()
    return sorted(root.glob("queue_*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True)[:8]


def _queue_row(queue_item_id, thread_id, *, queue_db=None):
    """Read exact queue metadata only; None means the native DB is unavailable."""
    if not (isinstance(queue_item_id, str) and QUEUE_ID.fullmatch(queue_item_id)
            and isinstance(thread_id, str) and UUID.fullmatch(thread_id)):
        return None
    try:
        paths = _queue_paths(queue_db)
    except OSError:
        return None
    if not paths:
        return None
    absent = True
    for path in paths:
        try:
            with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=.35)) as db:
                db.execute("PRAGMA query_only=ON")
                row = db.execute("SELECT payload_json FROM queued_items WHERE id=? AND thread_id=?",
                                 (queue_item_id, thread_id)).fetchone()
                if row:
                    return row[0]
        except (OSError, sqlite3.Error):
            absent = False
    return False if absent else None


def native_queue_item_present(queue_item_id, thread_id, *, queue_db=None):
    """True/False/None: exact item exists, is absent, or cannot be verified."""
    if not (isinstance(queue_item_id, str) and QUEUE_ID.fullmatch(queue_item_id)
            and isinstance(thread_id, str) and UUID.fullmatch(thread_id)):
        return None
    try:
        paths = _queue_paths(queue_db)
    except OSError:
        return None
    if not paths:
        return None
    unavailable = False
    for path in paths:
        try:
            with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
                db.execute("PRAGMA query_only=ON")
                row = db.execute("SELECT 1 FROM queued_items WHERE id=? AND thread_id=? LIMIT 1",
                                 (queue_item_id, thread_id)).fetchone()
                if row:
                    return True
        except (OSError, sqlite3.Error):
            unavailable = True
    return None if unavailable else False


def native_queue_client_id(queue_item_id, thread_id, expected_text, *, queue_db=None):
    """Bind CLI queue ID to native client ID after exact target/text verification."""
    raw = _queue_row(queue_item_id, thread_id, queue_db=queue_db)
    if not isinstance(raw, str):
        return None
    try:
        user = json.loads(raw)["UserInput"]
        parts = user["content"]
        client_id = user["client_id"]
    except (ValueError, TypeError, KeyError):
        return None
    if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
            or parts[0].get("type") != "text" or parts[0].get("text") != expected_text
            or not isinstance(client_id, str) or not QUEUE_ID.fullmatch(client_id)):
        return None
    return client_id


def dispatch_receipt(ledger_path, operation_id):
    """Return only persisted delivery IDs/status, including pre-migration rows."""
    identifier = str(operation_id)
    if not identifier.isdecimal() or len(identifier) > 24:
        return None
    path = Path(ledger_path)
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.35)) as db:
            db.execute("PRAGMA query_only=ON")
            columns = {row[1] for row in db.execute("PRAGMA table_info(dispatches)")}
            if not {"id", "thread_id", "status"}.issubset(columns):
                return None
            queue = "queue_item_id" if "queue_item_id" in columns else "NULL"
            client = "client_id" if "client_id" in columns else "NULL"
            row = db.execute(f"SELECT {queue},{client},thread_id,status FROM dispatches WHERE id=?",
                             (identifier,)).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return dict(zip(("queueItemId", "clientUserMessageId", "threadId", "status"), row)) if row else None


def dispatch_pending(record, rows, *, ledger_path, runner=queue_text, clock=time.time):
    """Return fallback only before any attempted dispatch; uncertainty is final."""
    now = int(clock())
    if not isinstance(record, dict) or type(record.get("id")) is not int or record["id"] < 0:
        return {"status": "fallback"}
    path = Path(ledger_path); path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    identifier, digest = str(record["id"]), fingerprint(record)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS dispatches (id TEXT PRIMARY KEY,digest TEXT NOT NULL,thread_id TEXT NOT NULL,status TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,queue_item_id TEXT,client_id TEXT)")
        db.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in db.execute("PRAGMA table_info(dispatches)")}
        for column in ("queue_item_id", "client_id"):
            if column not in columns:
                db.execute(f"ALTER TABLE dispatches ADD COLUMN {column} TEXT")
        previous = db.execute("SELECT digest,status,queue_item_id,client_id FROM dispatches WHERE id=?", (identifier,)).fetchone()
        if previous:
            if previous[0] != digest:
                db.commit(); return {"status": "uncertain", "error": "dispatch_conflict"}
            status = previous[1] if previous[1] in {"accepted", "accepted_wake_failed"} else "uncertain"
            if previous[1] == "dispatching":
                db.execute("UPDATE dispatches SET status='uncertain',updated_at=? WHERE id=?", (now, identifier))
            db.commit()
            return {"status": status, "deduplicated": True,
                    **({"queueItemId": previous[2]} if previous[2] else {}),
                    **({"clientUserMessageId": previous[3]} if previous[3] else {})}
        if not eligible(record, rows, now):
            db.commit(); return {"status": "fallback"}
        db.execute("INSERT INTO dispatches(id,digest,thread_id,status,created_at,updated_at) VALUES (?,?,?,'dispatching',?,?)",
                   (identifier, digest, record["selected_thread_id"], now, now))
        db.commit()  # Durable guard exists before starting the executable.
        status = "uncertain"
        queue_id = client_id = None
        def persist_receipt(native_queue_id, native_client_id):
            nonlocal status, queue_id, client_id
            if not isinstance(native_queue_id, str) or QUEUE_ID.fullmatch(native_queue_id) is None:
                raise ValueError("invalid_native_queue_id")
            if native_client_id is not None and (not isinstance(native_client_id, str)
                    or QUEUE_ID.fullmatch(native_client_id) is None):
                raise ValueError("invalid_native_client_id")
            # Autocommit makes this durable before queue_text calls wake_thread.
            db.execute("UPDATE dispatches SET status='accepted',queue_item_id=?,client_id=?,updated_at=? WHERE id=?",
                       (native_queue_id, native_client_id, int(clock()), identifier))
            queue_id, client_id, status = native_queue_id, native_client_id, "accepted"
        try:
            if runner is queue_text:
                returned = runner(record["selected_thread_id"], record["text"], on_receipt=persist_receipt)
            else:
                returned = runner(record["selected_thread_id"], record["text"])
            code, output = returned[:2]
            queue_id = queued_item_id(code, output, record["selected_thread_id"])
            if len(returned) > 2 and isinstance(returned[2], str) and QUEUE_ID.fullmatch(returned[2]):
                client_id = returned[2]
            if queue_id:
                status = "accepted"
        except QueuedWakeFailure as failure:
            status = "accepted_wake_failed"
            queue_id = getattr(failure, "queue_item_id", None)
            client_id = getattr(failure, "client_user_message_id", None)
        except (Exception, KeyboardInterrupt, SystemExit):
            # An exception after the durable queue receipt must not erase it
            # or permit a second send.
            if queue_id:
                status = "accepted_wake_failed"
        db.execute("UPDATE dispatches SET status=?,queue_item_id=?,client_id=?,updated_at=? WHERE id=?",
                   (status, queue_id, client_id, int(clock()), identifier))
        return {"status": status, "deduplicated": False,
                **({"queueItemId": queue_id} if queue_id else {}),
                **({"clientUserMessageId": client_id} if client_id else {})}
    finally:
        db.close()

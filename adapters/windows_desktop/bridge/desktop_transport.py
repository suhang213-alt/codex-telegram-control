"""Local receipt boundary for the owner-authenticated cloud MCP relay.

No HTTP client, service credentials, background poller or bot token is stored
here. The native desktop heartbeat calls the cloud MCP tools and this CLI.
"""
from __future__ import annotations

from contextlib import closing
import re
import time
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import urllib.parse

PANEL_MAX_AGE = 120
PANEL_PATTERN = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:lhr\.life|localhost\.run|trycloudflare\.com)/?")
MAX_PAYLOAD_BYTES = 65536
ERROR_CODES = {"invalid_input", "owner_mismatch", "delivery_conflict", "receipt_unavailable"}


class ReceiptError(Exception):
    def __init__(self, code):
        self.code = code


def decode_delivery(encoded):
    if not isinstance(encoded, str) or len(encoded) > MAX_PAYLOAD_BYTES * 3:
        raise ReceiptError("invalid_input")
    try:
        raw = urllib.parse.unquote(encoded, errors="strict")
        if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError()
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict) or set(value) != {"requestId", "update", "claimToken"}:
            raise ValueError()
        update = value["update"]
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if type(update_id) is not int or not 0 <= update_id <= 9007199254740991:
            raise ValueError()
        if value["requestId"] != "telegram_" + str(update_id):
            raise ValueError()
        claim = value["claimToken"]
        if not isinstance(claim, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", claim) is None:
            raise ValueError()
        if bool(isinstance(update.get("message"), dict)) == bool(isinstance(update.get("callback_query"), dict)):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, AttributeError):
        raise ReceiptError("invalid_input") from None


def verify_owner(update, current, expected_username):
    if not isinstance(current, dict):
        raise ReceiptError("owner_mismatch")
    callback = update.get("callback_query") or {}
    message = update.get("message") or callback.get("message") or {}
    user = callback.get("from") or message.get("from") or {}
    chat = message.get("chat") or {}
    if not all(isinstance(item, dict) for item in (callback, message, user, chat)):
        raise ReceiptError("owner_mismatch")
    owner = current.get("owner_user_id")
    if (type(owner) is not int or owner <= 0 or owner != current.get("owner_chat_id")
            or type(user.get("id")) is not int or type(chat.get("id")) is not int
            or user["id"] != owner or chat["id"] != owner or chat.get("type") != "private"
            or str(user.get("username") or "").casefold() != expected_username.casefold()):
        raise ReceiptError("owner_mismatch")


def _connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE TABLE IF NOT EXISTS receipts (
            request_id TEXT PRIMARY KEY, update_id INTEGER NOT NULL UNIQUE,
            update_digest TEXT NOT NULL, status TEXT NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def receive_delivery(delivery, *, ledger_path, process_update, now=None):
    """Claim durably before calling existing code; never retry uncertain effects.

    Only IDs, a content digest, status and timestamps enter the journal. The
    native inbox remains the sole command queue. A receipt marked accepted
    means handed to the existing handler, not task completion.
    """
    update = delivery["update"]
    request_id = delivery["requestId"]
    digest = hashlib.sha256(json.dumps(update, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode("utf-8")).hexdigest()
    timestamp = int(time.time() if now is None else now)
    db = _connect(ledger_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT update_digest,status FROM receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is not None:
            if row[0] != digest:
                raise ReceiptError("delivery_conflict")
            disposition = "deduplicated" if row[1] == "accepted" else "uncertain"
            if row[1] == "dispatching":
                db.execute("UPDATE receipts SET status='uncertain',updated_at=? WHERE request_id=?", (timestamp, request_id))
            db.commit()
            return receipt_result(request_id, disposition)
        db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?)",
                   (request_id, update["update_id"], digest, "dispatching", timestamp, timestamp))
        db.commit()  # Effects cannot start before this FULL synchronous commit.
        disposition = "accepted"
        try:
            process_update(update)
        except (Exception, KeyboardInterrupt, SystemExit):
            disposition = "uncertain"
        db.execute("UPDATE receipts SET status=?,updated_at=? WHERE request_id=?",
                   (disposition, timestamp, request_id))
        db.commit()
        return receipt_result(request_id, disposition)
    finally:
        db.close()


def receipt_result(request_id, disposition):
    if disposition == "uncertain":
        # Keep the original cloud delivery pending until the owner resolves
        # possible partial effects. A replay must neither dispatch nor ack it.
        return {"ok": False, "requestId": request_id, "disposition": "uncertain",
                "acknowledge": False, "error": "delivery_uncertain"}
    return {"ok": True, "requestId": request_id, "disposition": disposition, "acknowledge": True}


def transport_status(ledger_path):
    """Read-only counts; no command text, identifiers or state are exposed."""
    path = Path(ledger_path)
    counts = {"accepted": 0, "uncertain": 0}
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            for status, count in db.execute("SELECT status,COUNT(*) FROM receipts GROUP BY status"):
                if status == "accepted":
                    counts["accepted"] += count
                else:
                    counts["uncertain"] += count
    return {"ok": True, "transport": "native_mcp", "receiptJournalPresent": path.is_file(),
            "accepted": counts["accepted"], "uncertain": counts["uncertain"]}


def run_cli(action, *, encoded=None, ledger_path, state_provider=None, process_update=None,
            expected_username="example_owner", stdout=None):
    output = sys.stdout if stdout is None else stdout
    try:
        if action == "desktop_transport_status" and encoded is None:
            result = transport_status(ledger_path)
        elif action == "receive_desktop":
            delivery = decode_delivery(encoded)
            verify_owner(delivery["update"], state_provider(), expected_username)
            result = receive_delivery(delivery, ledger_path=ledger_path, process_update=process_update)
        else:
            raise ReceiptError("invalid_input")
        code = 0 if result["ok"] else 2
    except ReceiptError as error:
        result, code = {"ok": False, "error": error.code if error.code in ERROR_CODES else "receipt_unavailable"}, 2
    except (Exception, KeyboardInterrupt):
        result, code = {"ok": False, "error": "receipt_unavailable"}, 2
    print(json.dumps(result, separators=(",", ":")), file=output)
    return code


def miniapp_status(reader, *, now=None):
    """Read only the whitelisted tunnel fields through the internal decoder."""
    value = reader()
    if not isinstance(value, dict):
        raise ValueError("invalid_status")
    clock = time.time() if now is None else now
    timestamp = value.get("at")
    url = value.get("url")
    valid_url = isinstance(url, str) and PANEL_PATTERN.fullmatch(url) is not None
    fresh = type(timestamp) is int and 0 <= clock - timestamp <= PANEL_MAX_AGE
    healthy = bool(valid_url and fresh and value.get("healthy") is True)
    return {"ok": True, "healthy": healthy, "url": url if healthy else None,
            "checkedAt": timestamp if type(timestamp) is int and timestamp >= 0 else None}

"""Observe selected Codex results using Windows directory events, never polling.

Only authenticated, already dispatched text commands belong here. The journal
contains IDs, hashes and delivery phases; text stays in the existing bridge
accessors/cache. A reply with an uncertain outcome is not sent twice.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from latency_trace import mark

MAX_RECORD = 1024 * 1024
MAX_OVERSIZE_HEADER = 64 * 1024
FINAL_PHASES = {"final", "final_answer"}
_HEX = re.compile(r"[0-9a-f]{64}")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def timestamp(value):
    if type(value) in (int, float):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def validate(record):
    if not isinstance(record, dict) or type(record.get("id")) is not int or record["id"] < 0:
        return False
    client_id = record.get("clientUserMessageId")
    queue_id = record.get("queueItemId")
    return (record.get("kind") == "watch_dispatched" and record.get("observer") == "directory_events"
            and isinstance(record.get("selected_thread_id"), str)
            and _UUID.fullmatch(record["selected_thread_id"]) is not None
            and type(record.get("received_at")) in (int, float) and record["received_at"] > 0
            and isinstance(record.get("expected_text_hash"), str)
            and _HEX.fullmatch(record["expected_text_hash"]) is not None
            and (client_id is None or isinstance(client_id, str) and _CLIENT_ID.fullmatch(client_id) is not None)
            and (queue_id is None or isinstance(queue_id, str) and _CLIENT_ID.fullmatch(queue_id) is not None))


def completed_item(record):
    """Extract ONLY native completed user/final items, never reasoning/tools."""
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload") or {}
    item = payload.get("item") or {}
    if payload.get("type") != "item_completed" or not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind not in ("UserMessage", "AgentMessage"):
        return None
    if kind == "AgentMessage" and item.get("phase") not in FINAL_PHASES:
        return None
    turn_id, item_id = payload.get("turn_id"), item.get("id")
    stamp = timestamp(record.get("timestamp"))
    if not all(isinstance(value, str) and 0 < len(value) <= 150 for value in (turn_id, item_id)) or stamp is None:
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    parts = [part.get("text") for part in content if isinstance(part, dict)
             and part.get("type") in ("text", "Text") and isinstance(part.get("text"), str)]
    text = "\n".join(parts)
    if not text:
        return None
    client_id = item.get("client_id") if kind == "UserMessage" else None
    return {"kind": kind, "turnId": turn_id, "itemId": item_id, "text": text,
            "createdAt": stamp, "clientUserMessageId": client_id}


def _header_string(header, key):
    match = re.search(rb'"' + key + rb'"\s*:\s*("(?:\\.|[^"\\])*")', header)
    if match is None:
        return None
    try:
        value = json.loads(match[1])
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, str) else None


def _oversized_item(prefix):
    """Read bounded metadata only; never mistake an unparsed item for a result.

    Native JSONL puts the record/payload/item headers before item content. If
    that layout changes or the metadata exceeds the bound, the caller gets an
    unknown marker and must make the watch uncertain rather than skip it.
    """
    header = prefix[:MAX_OVERSIZE_HEADER]
    payload = re.search(rb'"payload"\s*:\s*\{', header)
    if payload is None:
        root_type = _header_string(header, b"type")
        if root_type is not None and root_type != "event_msg":
            return None
        return {"kind": "Oversized", "classification": "unknown"}
    root = header[:payload.start()]
    root_type = _header_string(root, b"type")
    if root_type is not None and root_type != "event_msg":
        return None
    item = re.search(rb'"item"\s*:\s*\{', header[payload.end():])
    if item is None:
        return {"kind": "Oversized", "classification": "unknown"}
    item_start = payload.end() + item.start()
    payload_header = header[payload.end():item_start]
    event_type = _header_string(payload_header, b"type")
    if event_type is not None and event_type != "item_completed":
        return None
    if root_type != "event_msg" or event_type != "item_completed":
        return {"kind": "Oversized", "classification": "unknown"}
    item_header = header[item_start + len(item[0]):]
    # Only metadata before any content/text value may identify the item. An
    # embedded quoted word inside a giant message cannot forge these fields.
    data_start = re.search(rb'"(?:content|text)"\s*:', item_header)
    if data_start:
        item_header = item_header[:data_start.start()]
    item_type = _header_string(item_header, b"type")
    if item_type not in ("UserMessage", "AgentMessage"):
        if item_type is not None:
            return None  # A huge tool/reasoning record is unrelated.
        return {"kind": "Oversized", "classification": "unknown"}
    return {"kind": "Oversized", "classification": item_type,
            "turnId": _header_string(payload_header, b"turn_id"),
            "itemId": _header_string(item_header, b"id"),
            "phase": _header_string(item_header, b"phase"),
            "clientUserMessageId": _header_string(item_header, b"client_id"),
            "createdAt": timestamp(_header_string(root, b"timestamp"))}


def _initial_offset(path, received_at):
    """Find the time boundary by scanning prefixes backwards, not tool bodies."""
    from miniapp_server import reverse_line_ranges
    with path.open("rb") as source:
        end = os.fstat(source.fileno()).st_size
        for start, stop in reverse_line_ranges(source, end):
            source.seek(start)
            prefix = source.read(min(2048, stop - start))
            match = re.search(rb'"timestamp"\s*:\s*"([^"\r\n]{1,64})"', prefix)
            stamp = timestamp(match[1].decode("ascii", "ignore")) if match else None
            if stamp is not None and stamp < received_at:
                return stop
    return 0


def _read_items(path, offset):
    """Read complete appended lines; leave a partial final line for next event."""
    rows = []
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        if offset > size:
            raise ValueError("rollout_truncated")
        source.seek(offset)
        while source.tell() < size:
            start = source.tell()
            line = source.readline(MAX_RECORD + 1)
            if len(line) > MAX_RECORD:
                oversized = _oversized_item(line)
                while line and not line.endswith(b"\n"):
                    line = source.readline(65536)
                if not line.endswith(b"\n"):
                    return rows, start
                offset = source.tell()
                if oversized is not None:
                    rows.append(oversized)
                continue
            if not line.endswith(b"\n"):
                return rows, start
            offset = source.tell()
            # The native item header precedes content. Avoid decoding giant
            # reasoning/tool payloads even when they fit MAX_RECORD.
            prefix = line[:2048]
            if b'"item_completed"' not in prefix or not re.search(rb'"type"\s*:\s*"(?:UserMessage|AgentMessage)"', prefix):
                continue
            try:
                item = completed_item(json.loads(line))
            except (ValueError, UnicodeDecodeError, TypeError):
                continue
            if item is not None:
                rows.append(item)
    return rows, offset


class WindowsDirectoryEvents:
    """One overlapped ReadDirectoryChangesW subscription for a directory.

    start() returns only after the native read is armed. Rearm precedes the
    callback, so file appends during processing are retained by Windows.
    """
    def __init__(self, directory, callback):
        if os.name != "nt":
            raise OSError("Windows directory notifications unavailable")
        self.directory, self.callback = Path(directory), callback
        self.ready, self.stopped = threading.Event(), threading.Event()
        self.error, self.handle, self.event = None, None, None
        self.thread = threading.Thread(target=self._run, name="codex-result-events", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            self.close()
            raise OSError("directory_event_registration_failed")

    def _run(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Overlapped), ctypes.c_void_p]
        k.ReadDirectoryChangesW.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        k.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.ResetEvent.argtypes = [wintypes.HANDLE]
        self._kernel = k
        try:
            self.handle = k.CreateFileW(str(self.directory), 1, 7, None, 3, 0x42000000, None)
            if self.handle == ctypes.c_void_p(-1).value:
                self.handle = None
                raise OSError("directory_open_failed")
            self.event = k.CreateEventW(None, True, False, None)
            if not self.event:
                raise OSError("directory_event_failed")
            buffer = ctypes.create_string_buffer(65536)
            ov = Overlapped(); ov.hEvent = self.event
            count = wintypes.DWORD()
            def arm():
                k.ResetEvent(self.event)
                if not k.ReadDirectoryChangesW(self.handle, buffer, len(buffer), False, 0x1 | 0x8 | 0x10,
                                                None, ctypes.byref(ov), None):
                    if ctypes.get_last_error() != 997:
                        raise OSError("directory_read_failed")
            arm()
            self.ready.set()
            while not self.stopped.is_set():
                if k.WaitForSingleObject(self.event, 0xFFFFFFFF) != 0:
                    raise OSError("directory_wait_failed")
                if self.stopped.is_set():
                    break
                if not k.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(count), False):
                    raise OSError("directory_result_failed")
                arm()  # Re-arm before processing; no polling interval.
                self.callback()
        except Exception:
            self.error = "directory_events_failed"
            self.ready.set()
            if not self.stopped.is_set():
                try:
                    self.callback()
                except Exception:
                    pass
        finally:
            if self.handle:
                k.CancelIoEx(self.handle, None)
                k.CloseHandle(self.handle)
                self.handle = None
            if self.event:
                k.CloseHandle(self.event)
                self.event = None

    def close(self):
        self.stopped.set()
        if self.handle and hasattr(self, "_kernel"):
            self._kernel.CancelIoEx(self.handle, None)
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(5)


class ResponseWatch:
    def __init__(self, *, ledger_path, resolve_path, deliver, pending_records=lambda: [],
                 events_factory=WindowsDirectoryEvents, file_events_factory=None,
                 on_failure=lambda record: None):
        self.ledger_path = Path(ledger_path)
        self.resolve_path, self.deliver, self.pending_records = resolve_path, deliver, pending_records
        self.events_factory = events_factory
        self.file_events_factory = file_events_factory
        self.on_failure = on_failure
        self.lock = threading.RLock()
        self.watches, self.directories, self.files = {}, {}, {}

    def _db(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.ledger_path), timeout=5, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY,digest TEXT NOT NULL,state TEXT NOT NULL,turn_id TEXT,user_id TEXT,final_id TEXT,client_id TEXT)")
        if "client_id" not in {row[1] for row in db.execute("PRAGMA table_info(watches)")}:
            db.execute("ALTER TABLE watches ADD COLUMN client_id TEXT")
        db.execute("CREATE TABLE IF NOT EXISTS user_bindings (thread_id TEXT NOT NULL,user_id TEXT NOT NULL,watch_id TEXT NOT NULL,PRIMARY KEY(thread_id,user_id),UNIQUE(watch_id))")
        return db

    def start(self):
        results = []
        for record in sorted(self.pending_records(), key=_record_order):
            if validate(record):
                results.append(self.watch(record))
        return results

    def watch(self, record):
        if not validate(record):
            return {"ok": False, "error": "invalid_watch"}
        identity = str(record["id"])
        digest_input = {k: record[k] for k in ("id", "selected_thread_id", "received_at", "expected_text_hash")}
        for key in ("queueItemId", "clientUserMessageId"):
            if record.get(key):
                digest_input[key] = record[key]
        digest = text_hash(json.dumps(digest_input, sort_keys=True))
        with self.lock:
            if identity in self.watches:
                return {"ok": self.watches[identity]["digest"] == digest, "disposition": "watching"}
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT digest,state,turn_id,user_id,client_id FROM watches WHERE id=?", (identity,)).fetchone()
                if old and old[0] != digest:
                    db.commit(); return {"ok": False, "error": "watch_conflict"}
                expected_client = record.get("clientUserMessageId")
                if not expected_client:
                    # Historical text/time bindings do not prove that this
                    # operation reached the chat. Retire them without a retry.
                    if old:
                        db.execute("UPDATE watches SET state='uncertain',turn_id=NULL,user_id=NULL,final_id=NULL WHERE id=?", (identity,))
                        db.execute("DELETE FROM user_bindings WHERE watch_id=?", (identity,))
                    else:
                        db.execute("INSERT INTO watches(id,digest,state) VALUES (?,?,'uncertain')", (identity, digest))
                    db.commit()
                    return {"ok": False, "disposition": "uncertain"}
                if old and old[4] != expected_client:
                    db.commit(); return {"ok": False, "error": "watch_conflict"}
                if old and old[1] in ("done", "delivering", "uncertain"):
                    db.commit()
                    return {"ok": old[1] == "done", "disposition": old[1]}
                path = self.resolve_path(record["selected_thread_id"])
                if path is None:
                    db.commit(); return {"ok": False, "error": "rollout_unavailable"}
                path = Path(path)
                existing_events = self.directories.get(path.parent)
                if existing_events is not None and getattr(existing_events, "error", None):
                    db.commit()
                    return {"ok": False, "error": "directory_subscription_failed"}
                db.execute("INSERT OR IGNORE INTO watches(id,digest,state,client_id) VALUES (?,?,'watching',?)",
                           (identity, digest, expected_client))
                if old and old[1] == "failed":
                    db.execute("UPDATE watches SET state='watching' WHERE id=?", (identity,))
                db.commit()
                entry = {"record": dict(record), "digest": digest, "path": path, "offset": None,
                         "turnId": old[2] if old else None, "userId": old[3] if old else None}
                self.watches[identity] = entry
                directory = path.parent
                if directory not in self.directories:
                    events = self.events_factory(directory, lambda directory=directory: self._directory_changed(directory))
                    self.directories[directory] = events
                    try:
                        events.start()
                    except Exception:
                        self.directories.pop(directory, None)
                        self.watches.pop(identity, None)
                        return {"ok": False, "error": "event_registration_failed"}
                if self.file_events_factory and path not in self.files:
                    events = self.file_events_factory(path, lambda path=path: self._file_changed(path))
                    self.files[path] = events
                    try:
                        events.start()
                    except Exception:
                        self.files.pop(path, None)
                        self.watches.pop(identity, None)
                        return {"ok": False, "error": "file_event_registration_failed"}
                # Subscription is live before taking any initial file snapshot.
                entry["offset"] = _initial_offset(path, record["received_at"])
                mark(identity, "registered")
                self._scan(identity, entry)
                return {"ok": True, "disposition": "watching"}
            finally:
                db.close()

    def _directory_changed(self, directory):
        observed_at = time.time()
        with self.lock:
            entries = sorted(self.watches.items(), key=lambda row: _record_order(row[1]["record"]))
            for identity, entry in entries:
                if entry["path"].parent == directory and entry["offset"] is not None:
                    if getattr(self.directories.get(directory), "error", None):
                        self._fail_before_reply(identity, entry)
                    else:
                        self._scan(identity, entry, event_at=observed_at)
        self._close_unused_files()

    def _file_changed(self, path):
        observed_at = time.time()
        with self.lock:
            entries = sorted(self.watches.items(), key=lambda row: _record_order(row[1]["record"]))
            for identity, entry in entries:
                if entry["path"] == path and entry["offset"] is not None:
                    mark(identity, "read_event", at=observed_at)
                    self._scan(identity, entry)
                    if identity in self.watches and getattr(self.files.get(path), "error", None):
                        self._fail_before_reply(identity, entry)
        self._close_unused_files()

    def _close_unused_files(self):
        # A callback can be waiting on our lock. Never join its thread while
        # holding that lock, or closing a subscription adds a five-second lag.
        with self.lock:
            used = {entry["path"] for entry in self.watches.values()}
            unused = [self.files.pop(path) for path in list(self.files) if path not in used]
        for events in unused:
            events.close()

    def _fail_before_reply(self, identity, entry):
        db = self._db()
        try:
            changed = db.execute("UPDATE watches SET state='failed' WHERE id=? AND state='watching'", (identity,))
            self.watches.pop(identity, None)
            if changed.rowcount:
                self.on_failure(entry["record"])
        finally:
            db.close()

    def _uncertain_oversized(self, db, identity):
        """Retain the original receipt, with no retry or claim of completion."""
        db.execute("UPDATE watches SET state='uncertain' WHERE id=? AND state='watching'", (identity,))
        self.watches.pop(identity, None)

    def _scan(self, identity, entry, event_at=None):
        db = self._db()
        try:
            rows, offset = _read_items(entry["path"], entry["offset"])
            entry["offset"] = offset
            expected = entry["record"]["expected_text_hash"]
            expected_client = entry["record"].get("clientUserMessageId")
            lower = entry["record"]["received_at"]
            for row in rows:
                if row["kind"] == "Oversized":
                    if row.get("createdAt") is not None and row["createdAt"] < lower:
                        continue
                    classification = row.get("classification")
                    if classification == "UserMessage":
                        client = row.get("clientUserMessageId")
                        if client and client != expected_client:
                            continue
                    elif classification == "AgentMessage":
                        phase, turn_id = row.get("phase"), row.get("turnId")
                        if phase is not None and phase not in FINAL_PHASES:
                            continue
                        if turn_id and entry["turnId"] and turn_id != entry["turnId"]:
                            continue
                        if turn_id and not entry["turnId"]:
                            continue
                    self._uncertain_oversized(db, identity)
                    return
                if row["createdAt"] < lower:
                    continue
                if row["kind"] == "UserMessage":
                    # A legacy receipt without native client ID cannot bind a
                    # turn by text/time alone, even if the SHA is identical.
                    if not expected_client or row.get("clientUserMessageId") != expected_client:
                        continue
                    if text_hash(row["text"]) != expected:
                        continue
                    if entry["userId"] and entry["userId"] != row["itemId"]:
                        continue
                    thread_id = entry["record"]["selected_thread_id"]
                    db.execute("INSERT OR IGNORE INTO user_bindings VALUES (?,?,?)", (thread_id, row["itemId"], identity))
                    owner = db.execute("SELECT watch_id FROM user_bindings WHERE thread_id=? AND user_id=?", (thread_id, row["itemId"])).fetchone()
                    if not owner or owner[0] != identity:
                        continue
                    entry["turnId"], entry["userId"] = row["turnId"], row["itemId"]
                    mark(identity, "user_matched", source_at=row["createdAt"])
                    db.execute("UPDATE watches SET turn_id=?,user_id=? WHERE id=?", (row["turnId"], row["itemId"], identity))
                elif entry["turnId"] == row["turnId"]:
                    if event_at is not None:
                        mark(identity, "directory_event", at=event_at)
                    mark(identity, "final_detected", source_at=row["createdAt"])
                    # Record the exact item before any externally visible reply.
                    claim = db.execute("UPDATE watches SET state='delivering',final_id=? WHERE id=? AND state='watching'", (row["itemId"], identity))
                    self.watches.pop(identity, None)
                    if claim.rowcount != 1:
                        return
                    try:
                        mark(identity, "delivery_start")
                        self.deliver(entry["record"], {**row, "threadId": entry["record"]["selected_thread_id"]})
                    except Exception:
                        db.execute("UPDATE watches SET state='uncertain' WHERE id=?", (identity,))
                        return
                    db.execute("UPDATE watches SET state='done' WHERE id=?", (identity,))
                    return
        except Exception:
            # No delivery has started: native worker may safely wait/read the
            # already dispatched command. It must never dispatch it again.
            self._fail_before_reply(identity, entry)
        finally:
            db.close()

    def status(self):
        with self.lock:
            db = self._db()
            try:
                states = dict(db.execute("SELECT state,count(*) FROM watches GROUP BY state"))
            finally:
                db.close()
            return {"watching": len(self.watches), "directories": len(self.directories), "files": len(self.files), "states": states,
                    "eventErrors": sum(bool(getattr(item, "error", None)) for item in [*self.directories.values(), *self.files.values()])}

    def close(self):
        with self.lock:
            events = [*self.directories.values(), *self.files.values()]
            self.directories.clear(); self.files.clear(); self.watches.clear()
        for item in events:
            item.close()


def bridge_delivery(bridge, record, result):
    from urllib.parse import quote
    from miniapp_server import local_history_page
    mark(record["id"], "history_start")
    page = local_history_page(result["threadId"])
    mark(record["id"], "history_done")
    if page is None:
        raise ValueError("result_history_unavailable")
    messages = list(page["messages"])
    # item_completed can precede the corresponding response_item append.
    # Include the exact completed item now rather than waiting for another event.
    if not any(row.get("id") == result["itemId"] for row in messages):
        messages.append({"id": result["itemId"], "role": "assistant", "text": result["text"],
                         "createdAt": result["createdAt"], "attachments": []})
    mark(record["id"], "cache_start")
    bridge.cache_snapshot("cache_thread", quote(json.dumps({"id": result["threadId"], "messages": messages[-30:],
                                  "nextCursor": page.get("nextCursor")}, ensure_ascii=False), safe="~"))
    mark(record["id"], "cache_done")
    reply = {"operationId": record["id"], "ok": True, "text": result["text"][:3900],
             "buttons": [[{"text": "Открыть переписку", "callback_data": "view:" + result["threadId"]}]]}
    mark(record["id"], "reply_start")
    bridge.reply_payload(reply)
    mark(record["id"], "reply_done")
    bridge.ack_pending(record["id"])
    mark(record["id"], "ack_done")


_SERVICE_LOCK = threading.Lock()
_SERVICES = {}


def _record_order(record):
    stamp = record.get("received_at", 0)
    identifier = record.get("id", 0)
    return (stamp if type(stamp) in (int, float) else 0, identifier if type(identifier) is int else 0)


class BridgeObserver:
    """Boolean registration interface used by the long-lived bridge process."""
    def __init__(self, bridge, service):
        self.bridge, self.service = bridge, service

    def watch(self, record):
        result = self.service.watch(record)
        self.service._close_unused_files()
        # A prior ambiguous reply retains ownership of the pending receipt. It
        # must not fall through to a second reply from the model worker.
        return bool(result.get("ok") or result.get("disposition") in ("delivering", "uncertain"))

    def start(self):
        accepted = released = 0
        for record in sorted(self.bridge.pending_records(), key=_record_order):
            if record.get("kind") != "watch_dispatched" or record.get("observer") != "directory_events":
                continue
            try:
                ok = self.watch(record)
            except Exception:
                ok = False
            if ok:
                accepted += 1
            else:
                self.bridge.release_response_observer(record["id"])
                released += 1
        return {"accepted": accepted, "released": released}

    def close(self):
        self.service.close()

    def status(self):
        return self.service.status()


def observer_ledger_path(bridge):
    return Path(bridge.APP_DIR) / "response_watch.sqlite3"


def get_service(bridge):
    key = str(bridge.APP_DIR)
    with _SERVICE_LOCK:
        if key not in _SERVICES:
            from miniapp_server import local_codex_rollout
            from windows_read_events import WindowsReadEvents
            service = ResponseWatch(ledger_path=observer_ledger_path(bridge),
                resolve_path=local_codex_rollout, pending_records=bridge.pending_records,
                file_events_factory=WindowsReadEvents,
                deliver=lambda record, result: bridge_delivery(bridge, record, result),
                on_failure=lambda record: bridge.release_response_observer(record["id"]))
            _SERVICES[key] = BridgeObserver(bridge, service)
        return _SERVICES[key]


def ledger_status(ledger_path, operation_id=None):
    """Read-only CLI evidence: no watcher, keys, cache or conversation content."""
    path = Path(ledger_path)
    report = {"ok": True, "observedAt": time.time(), "ledgerExists": path.is_file(), "states": {}}
    if not path.is_file():
        if operation_id is not None:
            report["operation"] = None
        return report
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        report["states"] = dict(db.execute("SELECT state,count(*) FROM watches GROUP BY state"))
        if operation_id is not None:
            columns = {row[1] for row in db.execute("PRAGMA table_info(watches)")}
            client_column = "client_id" if "client_id" in columns else "NULL"
            row = db.execute(f"SELECT id,state,turn_id,user_id,final_id,{client_column} FROM watches WHERE id=?",
                             (str(operation_id),)).fetchone()
            if row:
                # Pre-migration rows could bind a turn by matching text/time.
                # They cannot establish receipt for this particular dispatch.
                state = row[1] if row[5] else "uncertain"
                proof = row[2:5] if row[5] else (None, None, None)
                report["operation"] = dict(zip(("id", "state", "turnId", "userItemId", "finalItemId"),
                                               (row[0], state, *proof)))
            else:
                report["operation"] = None
        return report
    finally:
        db.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Read-only response observer metadata")
    parser.add_argument("action", choices=["status"])
    parser.add_argument("--operation-id", type=int)
    args = parser.parse_args()
    if args.operation_id is not None and args.operation_id < 0:
        parser.error("operation id must be non-negative")
    try:
        import telegram_bridge as bridge
        report = ledger_status(observer_ledger_path(bridge), args.operation_id)
    except Exception:
        report = {"ok": False, "error": "observer_status_unavailable"}
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

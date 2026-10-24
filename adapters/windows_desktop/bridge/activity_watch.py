"""Event-driven handoff of local Codex user-facing messages to the native worker.

The rollout is used only to identify completed message IDs and their order.
The worker obtains the actual text and any answer choices through read_thread.
"""
from __future__ import annotations
import bridge_config as config

import json
from pathlib import Path
import sqlite3
import threading
import time

from response_watch import WindowsDirectoryEvents
from wake_dispatcher import worker_thread_id

LEGACY_SERVICE_THREAD_ID = config.service_thread_id()
PHASES = {"commentary", "final", "final_answer"}
MAX_LINE = 1024 * 1024


def event_from_line(line: bytes):
    if b'"item_completed"' not in line[:2048] or b'"AgentMessage"' not in line[:2048]:
        return None
    try:
        record = json.loads(line)
        payload = record.get("payload") or {}
        item = payload.get("item") or {}
        has_choices = bool(item.get("questions"))
        native_async = item.get("delivery") == "async" and has_choices
        if (record.get("type") != "event_msg" or payload.get("type") != "item_completed"
                or item.get("type") != "AgentMessage" or
                (item.get("phase") not in PHASES and not native_async)):
            return None
        item_id = item.get("id")
        if not isinstance(item_id, str) or not 0 < len(item_id) <= 150:
            return None
        return {"itemId": item_id, "phase": item.get("phase"),
                "hasChoices": has_choices, "sourceAt": record.get("timestamp")}
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


class ActivityWatch:
    def __init__(self, *, bridge, resolve_path, ledger_path, events_factory=WindowsDirectoryEvents,
                 notify=None):
        self.bridge, self.resolve_path = bridge, resolve_path
        self.ledger_path = Path(ledger_path)
        self.events_factory = events_factory
        self.notify = notify
        self.excluded_threads = {worker_thread_id(), LEGACY_SERVICE_THREAD_ID}
        self.lock = threading.RLock()
        self.closed = False
        self.paths = {}
        self.directories = {}

    def coverage(self, thread_id):
        """Coverage of AgentMessage events only; not terminal/error/attention events."""
        with self.lock:
            alive = not self.closed
            entry = self.paths.get(thread_id) if alive else None
            watcher = self.directories.get(entry["path"].parent) if entry else None
            thread = getattr(watcher, "thread", None)
            return {"alive": alive, "covered": bool(watcher is not None and
                    not getattr(watcher, "error", None) and
                    (thread is None or thread.is_alive()))}

    def _db(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.ledger_path, timeout=5, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS events (thread_id TEXT NOT NULL,item_id TEXT NOT NULL,"
                   "operation_id INTEGER NOT NULL UNIQUE,PRIMARY KEY(thread_id,item_id))")
        db.execute("CREATE TABLE IF NOT EXISTS offsets (thread_id TEXT PRIMARY KEY,path TEXT NOT NULL,offset INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        return db

    def sync(self):
        """Register existing local threads; new threads are added on the next metadata sync."""
        with self.lock:
            db = self._db()
            try:
                baseline_row = db.execute("SELECT value FROM meta WHERE key='baseline_at'").fetchone()
                if baseline_row is None:
                    baseline = time.time()
                    db.execute("INSERT INTO meta VALUES ('baseline_at',?)", (str(baseline),))
                else:
                    baseline = float(baseline_row[0])
            finally:
                db.close()
            known_chatgpt_ids = getattr(self.bridge, "known_chatgpt_ids", None)
            chatgpt_ids = known_chatgpt_ids() if callable(known_chatgpt_ids) else set()
            rows = {}
            for row in self.bridge.local_codex_threads():
                if (isinstance(row, dict) and isinstance(row.get("id"), str) and
                        row.get("kind") == "codex" and row["id"] not in chatgpt_ids):
                    rows[row["id"]] = row
            # Native-visible Codex chats can have thread_source=agent_created_thread.
            # They are absent from local_codex_threads, which intentionally excludes
            # hidden subagents. Add only exact native-verified, in-scope local rows.
            cached_threads = getattr(self.bridge, "cached_threads", None)
            in_scope = getattr(self.bridge, "thread_in_scope", None)
            if callable(cached_threads) and callable(in_scope) and callable(known_chatgpt_ids):
                for row in cached_threads():
                    if not isinstance(row, dict):
                        continue
                    thread_id = row.get("id")
                    if (not isinstance(thread_id, str) or thread_id in rows or
                            thread_id in self.paths or thread_id in chatgpt_ids or
                            row.get("kind") != "codex" or
                            row.get("hostId") not in (None, "local") or
                            row.get("native_verification") != "codex_native_tools"):
                        continue
                    if in_scope(thread_id):
                        rows[thread_id] = row
            for row in rows.values():
                thread_id = row.get("id")
                if thread_id in self.excluded_threads or thread_id in self.paths:
                    continue
                path = self.resolve_path(thread_id)
                if path is None:
                    continue
                path = Path(path)
                try:
                    info = path.stat()
                except OSError:
                    continue
                db = self._db()
                try:
                    previous = db.execute("SELECT path,offset FROM offsets WHERE thread_id=?", (thread_id,)).fetchone()
                    offset = (previous[1] if previous and previous[0] == str(path)
                              else 0 if info.st_ctime > baseline else info.st_size)
                    db.execute("INSERT OR REPLACE INTO offsets VALUES (?,?,?)", (thread_id, str(path), offset))
                finally:
                    db.close()
                self.paths[thread_id] = {"path": path, "offset": offset}
                if path.parent not in self.directories:
                    watcher = self.events_factory(path.parent,
                        lambda directory=path.parent: self._changed(directory))
                    try:
                        watcher.start()
                    except Exception:
                        self.paths.pop(thread_id, None)
                        continue
                    self.directories[path.parent] = watcher
                # Close the registration race: process bytes appended after the
                # durable offset even if the change happened before subscription.
                self._scan(thread_id, self.paths[thread_id])

    def _changed(self, directory):
        with self.lock:
            for thread_id, entry in list(self.paths.items()):
                if entry["path"].parent == directory:
                    self._scan(thread_id, entry)
        # File changes also cover terminal/error/attention transitions without
        # agent text. The background worker confirms them through native tools.
        if self.notify and Path(__file__).with_name(".background_native_enabled").is_file():
            self.notify(None)

    def _scan(self, thread_id, entry):
        try:
            with entry["path"].open("rb") as source:
                if entry["offset"] > source.seek(0, 2):
                    entry["offset"] = 0
                source.seek(entry["offset"])
                while True:
                    line_start = source.tell()
                    line = source.readline(MAX_LINE + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE:
                        # A large tool output is irrelevant to message delivery.
                        # Drain it in bounded chunks so it cannot permanently
                        # block every later AgentMessage in the rollout.
                        while line and not line.endswith(b"\n"):
                            line = source.readline(64 * 1024)
                        if not line:
                            # The writer has not finished this line yet. Resume
                            # from its start when another file change arrives.
                            break
                        entry["offset"] = source.tell()
                        continue
                    if not line.endswith(b"\n"):
                        break
                    entry["offset"] = source.tell()
                    event = event_from_line(line)
                    if event:
                        event["source_offset"] = line_start
                        event["source_size"] = len(line)
                        self._queue(thread_id, event)
                db = self._db()
                try:
                    db.execute("UPDATE offsets SET offset=? WHERE thread_id=?", (entry["offset"], thread_id))
                finally:
                    db.close()
        except OSError:
            return

    def _queue(self, thread_id, event):
        if thread_id in self.excluded_threads:
            return
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM events WHERE thread_id=? AND item_id=?",
                          (thread_id, event["itemId"])).fetchone():
                db.commit()
                return
            # Keep the ID below JavaScript's exact-integer limit for native tools.
            operation_id = max(time.time_ns() // 1000,
                               (db.execute("SELECT MAX(operation_id) FROM events").fetchone()[0] or 0) + 1)
            while (self.bridge.INBOX / f"{operation_id}.bin").exists():
                operation_id += 1
            record = {"id": operation_id, "kind": "activity_event",
                      "selected_thread_id": thread_id, "source_item_id": event["itemId"],
                      "phase": event["phase"], "has_choices": event["hasChoices"],
                      "source_at": event["sourceAt"], "received_at": time.time()}
            if event["hasChoices"] and isinstance(event.get("source_offset"), int) and \
                    isinstance(event.get("source_size"), int):
                record["source_offset"] = event["source_offset"]
                record["source_size"] = event["source_size"]
            self.bridge.atomic_json(self.bridge.INBOX / f"{operation_id}.bin", record)
            db.execute("INSERT INTO events VALUES (?,?,?)", (thread_id, event["itemId"], operation_id))
            db.commit()
        finally:
            db.close()
        if self.notify:
            self.notify(operation_id)

    def close(self):
        with self.lock:
            self.closed = True
            watchers = list(self.directories.values())
            self.directories.clear()
            self.paths.clear()
        for watcher in watchers:
            watcher.close()

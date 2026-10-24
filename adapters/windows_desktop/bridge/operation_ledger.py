"""Isolated metadata-only checkpoints for an authenticated bridge dispatcher.

No bridge imports, network, prompts, queue inspection or execution live here.
The caller must commit begin_execute/begin_reply BEFORE the corresponding
external call, then confirm the outcome only from authoritative evidence.
After a restart, in-flight calls become uncertain and cannot be retried.

One process owns the ledger for its lifetime. The OS releases the lock on
process death; age alone never permits another owner to recover live work.
Use a local filesystem: this lock is not a distributed/cloud-sync lease.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time


class LedgerBusy(RuntimeError):
    """Another live process/instance owns this database."""


class InvalidRecord(ValueError):
    """Only bounded identifiers and fingerprints may be persisted."""


class OperationConflict(ValueError):
    """An operation ID was reused for a different request or target."""


class InvalidTransition(RuntimeError):
    """The requested action would skip a checkpoint or repeat an effect."""


_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_RECEIPT_KEYS = frozenset({
    "threadId", "clientThreadId", "hostId", "turnId", "itemId", "queueId", "status",
    "baselineTurnId", "baselineItemId", "promptDigest",
    "clientUserMessageId", "deliveryTransport",
})
_WATCH_STATES = frozenset({"effect_confirmed", "replying", "replied", "acked", "uncertain"})
_WATCH_STATUSES = frozenset({"sent", "created_codex", "created_work"})
_CURSOR = re.compile(r"[\x21-\x7e]{1,8192}")
_NEXT_ACTION = {
    "claimed": "execute",
    "executing": "in_flight",
    "effect_confirmed": "reply",
    "replying": "in_flight",
    "replied": "ack",
    "acked": "none",
    "uncertain": "manual_review",
}


@dataclass(frozen=True)
class Operation:
    operation_id: str
    fingerprint: str
    target: str
    state: str
    effect_id: str | None
    receipt: dict[str, str]
    created_at_ns: int
    updated_at_ns: int

    @property
    def next_action(self) -> str:
        return _NEXT_ACTION[self.state]


@dataclass(frozen=True)
class Claim:
    created: bool
    operation: Operation


def _identifier(value):
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise InvalidRecord("identifier_required")
    return value


def _receipt_json(receipt):
    if receipt is None:
        return "{}"
    if type(receipt) is not dict or not set(receipt).issubset(_RECEIPT_KEYS):
        raise InvalidRecord("receipt_metadata_required")
    for key, value in receipt.items():
        if key == "promptDigest":
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise InvalidRecord("sha256_prompt_digest_required")
        else:
            _identifier(value)
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"))


class _OwnerLock:
    def __init__(self, path):
        self.stream = open(path, "a+b")
        try:
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise LedgerBusy("ledger_owned") from None

    def close(self):
        if self.stream.closed:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
        # Never unlink the lock file: a new inode would split live ownership.


class BackgroundLedger:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._pid = os.getpid()
        self._db = None
        self._owner = _OwnerLock(self.path.with_name(self.path.name + ".lock"))
        try:
            self._db = sqlite3.connect(
                self.path, timeout=5, isolation_level=None, check_same_thread=False,
            )
            self._db.row_factory = sqlite3.Row
            mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            self._db.execute("PRAGMA synchronous=FULL")
            if mode.casefold() != "wal":
                raise RuntimeError("wal_required")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY NOT NULL,
                    fingerprint TEXT NOT NULL,
                    target TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'claimed','executing','effect_confirmed','replying',
                        'replied','acked','uncertain')),
                    effect_id TEXT,
                    receipt_json TEXT NOT NULL DEFAULT '{}',
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS watch_done (
                    operation_id TEXT PRIMARY KEY NOT NULL,
                    completed_at_ns INTEGER NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS watch_cursors (
                    operation_id TEXT PRIMARY KEY NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                )
            """)
            # The lifetime OS lock proves the old owner is no longer running.
            with self._transaction() as db:
                db.execute(
                    "UPDATE operations SET state='uncertain',updated_at_ns=? "
                    "WHERE state IN ('executing','replying')",
                    (time.time_ns(),),
                )
        except BaseException:
            self.close()
            raise

    def _check_open(self):
        if os.getpid() != self._pid:
            raise RuntimeError("ledger_owner_process_changed")
        if self._db is None:
            raise RuntimeError("ledger_closed")

    @contextmanager
    def _transaction(self):
        with self._mutex:
            self._check_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    @staticmethod
    def _operation(row):
        if row is None:
            return None
        return Operation(
            operation_id=row["operation_id"], fingerprint=row["fingerprint"],
            target=row["target"], state=row["state"], effect_id=row["effect_id"],
            receipt=json.loads(row["receipt_json"]),
            created_at_ns=row["created_at_ns"], updated_at_ns=row["updated_at_ns"],
        )

    def get(self, operation_id: str) -> Operation | None:
        _identifier(operation_id)
        with self._mutex:
            self._check_open()
            return self._operation(self._db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,),
            ).fetchone())

    def claim(self, operation_id: str, fingerprint: str, target: str) -> Claim:
        _identifier(operation_id)
        _identifier(target)
        if type(fingerprint) is not str or _DIGEST.fullmatch(fingerprint) is None:
            raise InvalidRecord("sha256_fingerprint_required")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint or row["target"] != target:
                    raise OperationConflict("operation_identity_conflict")
                return Claim(False, self._operation(row))
            stamp = time.time_ns()
            db.execute(
                "INSERT INTO operations "
                "(operation_id,fingerprint,target,state,created_at_ns,updated_at_ns) "
                "VALUES (?,?,?,'claimed',?,?)",
                (operation_id, fingerprint, target, stamp, stamp),
            )
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
            return Claim(True, self._operation(row))

    def _transition(self, operation_id, expected, target, *,
                    effect_id=None, receipt_json=None, idempotent=False):
        _identifier(operation_id)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if row is None:
                raise InvalidTransition("unknown_operation")
            if idempotent and row["state"] == target:
                return self._operation(row)
            if row["state"] not in expected:
                raise InvalidTransition("checkpoint_not_permitted")
            if target == "effect_confirmed":
                db.execute(
                    "UPDATE operations SET state=?,effect_id=?,receipt_json=?,"
                    "updated_at_ns=? WHERE operation_id=?",
                    (target, effect_id, receipt_json, time.time_ns(), operation_id),
                )
            else:
                db.execute(
                    "UPDATE operations SET state=?,updated_at_ns=? WHERE operation_id=?",
                    (target, time.time_ns(), operation_id),
                )
            return self._operation(db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,),
            ).fetchone())

    def begin_execute(self, operation_id: str) -> Operation:
        """Commit BEFORE the native mutation. Repeated grants are forbidden."""
        return self._transition(operation_id, {"claimed"}, "executing")

    def confirm_effect(self, operation_id: str, *, receipt: dict | None = None,
                       effect_id: str | None = None) -> Operation:
        """Commit authoritative success and bounded recovery IDs together."""
        if effect_id is not None:
            _identifier(effect_id)
        serialized = _receipt_json(receipt)
        return self._transition(
            operation_id, {"executing"}, "effect_confirmed",
            effect_id=effect_id, receipt_json=serialized,
        )

    def begin_reply(self, operation_id: str) -> Operation:
        """Commit BEFORE externally sending a reply/notice."""
        return self._transition(operation_id, {"effect_confirmed"}, "replying")

    def confirm_reply(self, operation_id: str) -> Operation:
        """Call only when delivery has been authoritatively confirmed."""
        return self._transition(operation_id, {"replying"}, "replied")

    def confirm_ack(self, operation_id: str) -> Operation:
        """Record an ACK already completed by the queue owner.

        A crash after queue ACK but before this call leaves 'replied'; recovery
        may repeat only that idempotent queue ACK, never the effect or reply.
        """
        return self._transition(operation_id, {"replied"}, "acked", idempotent=True)

    def mark_uncertain(self, operation_id: str) -> Operation:
        """Persist unknown outcome; no method resets it for automatic retry."""
        return self._transition(
            operation_id, {"executing", "replying"}, "uncertain", idempotent=True,
        )

    @staticmethod
    def _watchable(row):
        return (
            row is not None
            and row["state"] in _WATCH_STATES
            and json.loads(row["receipt_json"]).get("status") in _WATCH_STATUSES
        )

    def active_watches(self) -> list[dict]:
        """Recover observation from an already confirmed effect, without replay.

        A reply failure may make the operation uncertain after its native
        effect was confirmed. Its receipt still proves that observation is
        safe; an uncertain native effect has no such receipt and is excluded.
        The cursor is native opaque metadata, never prompt/message content.
        """
        with self._mutex:
            self._check_open()
            rows = self._db.execute(
                "SELECT o.*,c.cursor AS watch_cursor FROM operations o "
                "LEFT JOIN watch_done d ON d.operation_id=o.operation_id "
                "LEFT JOIN watch_cursors c ON c.operation_id=o.operation_id "
                "WHERE d.operation_id IS NULL "
                "ORDER BY o.created_at_ns,o.operation_id",
            ).fetchall()
            return [{
                "operation_id": row["operation_id"],
                "target": row["target"],
                "receipt": json.loads(row["receipt_json"]),
                "created_at": row["created_at_ns"] / 1_000_000_000,
                "afterCursor": row["watch_cursor"],
            } for row in rows if self._watchable(row)]

    def _require_watch(self, db, operation_id):
        row = db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,),
        ).fetchone()
        if not self._watchable(row):
            raise InvalidTransition("confirmed_watch_required")

    def save_watch_cursor(self, operation_id: str, cursor: str) -> None:
        """Commit only an opaque cursor obtained from native wait_threads."""
        _identifier(operation_id)
        if type(cursor) is not str or _CURSOR.fullmatch(cursor) is None:
            raise InvalidRecord("native_watch_cursor_required")
        with self._transaction() as db:
            self._require_watch(db, operation_id)
            if db.execute(
                "SELECT 1 FROM watch_done WHERE operation_id=?", (operation_id,),
            ).fetchone():
                raise InvalidTransition("watch_already_finished")
            db.execute(
                "INSERT INTO watch_cursors(operation_id,cursor,updated_at_ns) VALUES(?,?,?) "
                "ON CONFLICT(operation_id) DO UPDATE SET "
                "cursor=excluded.cursor,updated_at_ns=excluded.updated_at_ns",
                (operation_id, cursor, time.time_ns()),
            )

    def finish_watch(self, operation_id: str) -> None:
        """Durably stop observation after exact terminal evidence; idempotent."""
        _identifier(operation_id)
        with self._transaction() as db:
            self._require_watch(db, operation_id)
            db.execute(
                "INSERT OR IGNORE INTO watch_done(operation_id,completed_at_ns) VALUES(?,?)",
                (operation_id, time.time_ns()),
            )
            db.execute(
                "DELETE FROM watch_cursors WHERE operation_id=?", (operation_id,),
            )

    def durability(self) -> dict:
        with self._mutex:
            self._check_open()
            return {
                "journal_mode": self._db.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": self._db.execute("PRAGMA synchronous").fetchone()[0],
            }

    def close(self):
        with self._mutex:
            if os.getpid() != self._pid:
                raise RuntimeError("ledger_owner_process_changed")
            try:
                if self._db is not None:
                    self._db.close()
                    self._db = None
            finally:
                self._owner.close()

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *_exc):
        self.close()

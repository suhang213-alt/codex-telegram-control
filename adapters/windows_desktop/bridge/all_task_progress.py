"""Read-only, bounded local task progress. Call only after owner/thread auth.

Metadata/path providers are trusted backend dependencies, never request payloads.
No history reader, network, model, command execution or filesystem writes exist here.
"""
from __future__ import annotations
import bridge_config as config

from collections import OrderedDict
from contextlib import closing
import codecs
import datetime as dt
import json
from itertools import islice
import math
from pathlib import Path
import re
import sqlite3
import threading

MSK = dt.timezone(dt.timedelta(hours=3))
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
PUBLIC = {"user", "chatgpt_handoff", "avatar_quick_chat", "agent_forked_thread", "agent_created_thread"}
EXCLUDED = {config.service_thread_id()}
MAX_INITIAL, MAX_APPEND, MAX_LINE, MAX_JSON = 4 * 1024**2, 256 * 1024, 256 * 1024, 256 * 1024
MAX_RECENT_INPUT_SCAN = 4 * 1024**2
_PRIVATE_TEXT = re.compile(r"(?:[a-zA-Z]:[\\/]|\\\\|/(?:home|Users|etc)/)\S*|https?://\S*|"
                           r"\b(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S+|"
                           r"\bsk-[a-zA-Z0-9_-]{12,}|\b\d{6,}:[a-zA-Z0-9_-]{20,}|"
                           r"\b(?:powershell|cmd\.exe|pythonw?(?:\.exe)?|curl|wget)\s+.*", re.I)
_PHRASE = re.compile(r"(?m)^[ \t]{0,3}Прогресс:\s*выполнено\s*(\d{1,4})\s*из\s*(\d{1,4})\s*задач\b", re.I)


def _loads(value):
    def pairs(items):
        result = {}
        for key, child in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = child
        return result
    def invalid(_):
        raise ValueError("nonfinite_json_number")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def _public_lines(value):
    result, fenced = [], False
    for line in value.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and not line.lstrip().startswith(">") and not line.startswith("    "):
            result.append(line)
    return "\n".join(result)


def _time(value):
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
        return result.astimezone(MSK) if result is not None and result.tzinfo is not None else None
    except ValueError:
        return None


def _safe(value, limit=600):
    if not isinstance(value, str):
        return ""
    return _PRIVATE_TEXT.sub("[скрыто]", "".join(c for c in value if ord(c) >= 32)).strip()[:limit]


def _child(row):
    if any(row.get(k) for k in ("agent_path", "agent_role", "parent_thread_id", "parent_id")):
        return True
    source = row.get("source")
    if isinstance(source, str) and source.casefold().startswith("subagent"):
        return True
    if isinstance(source, str) and source.startswith("{"):
        try:
            source = _loads(source) if len(source) <= 4096 else {"subagent": True}
        except ValueError:
            return True
    return isinstance(source, dict) and any(k in source for k in ("subagent", "parent_thread_id", "thread_spawn"))


def _native_public(row, identifier):
    return (isinstance(row, dict) and row.get("id") == identifier and row.get("kind") == "codex"
            and row.get("hostId", "local") == "local" and not _child(row)
            and row.get("native_verification", row.get("nativeProvenance")) in
            {"codex_native_tools", "local_codex_database", "native"})


def _normal_path(value, *, trusted_cwd=False, trusted_rollout=False):
    # Native Windows metadata uses extended local drive paths. Normalize only
    # trusted DB fields; callers still verify the resolved workspace/session.
    # UNC and device paths remain rejected.
    if (trusted_cwd or trusted_rollout) and isinstance(value, (str, Path)) and re.match(r"^\\\\\?\\[A-Za-z]:\\", str(value)):
        value = str(value)[4:]
    if not isinstance(value, (str, Path)) or str(value).startswith(("\\\\", "//")):
        raise ValueError("unsafe_path")
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path.absolute():
        raise ValueError("unsafe_path")
    return path.resolve(strict=True)


class _RecordEnvelope:
    """Bounded JSON projection for oversized records; never exposes body text.

    Validate every byte, but replace strings over 1024 bytes with an empty
    string. The final strict decoder checks grammar and duplicate keys. Only
    short, known envelope fields are consumed by the caller.
    """
    def __init__(self, limit):
        self.limit, self.output = limit, bytearray()
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.string, self.token, self.escape, self.unicode_left = False, None, False, 0
        self.depth, self.nodes = 0, 0

    def _put(self, value):
        if len(self.output) + len(value) > self.limit:
            raise ValueError("envelope_too_large")
        self.output.extend(value)

    def feed(self, data):
        self.decoder.decode(data, final=False)
        for char in data:
            if self.string:
                closed = False
                if self.unicode_left:
                    if char not in b"0123456789abcdefABCDEF":
                        raise ValueError("invalid_unicode_escape")
                    self.unicode_left -= 1
                elif self.escape:
                    if char not in b'"\\/bfnrtu':
                        raise ValueError("invalid_escape")
                    self.unicode_left = 4 if char == 117 else 0
                    self.escape = False
                elif char == 92:
                    self.escape = True
                elif char == 34:
                    closed = True
                elif char < 32:
                    raise ValueError("invalid_string")
                if self.token is not None:
                    self.token.append(char)
                    if len(self.token) > 1024:
                        self.token = None
                if closed:
                    self._put(self.token if self.token is not None else b'""')
                    self.string, self.token = False, None
            elif char == 34:
                self.string, self.token = True, bytearray(b'"')
            elif char in b" \t\r":
                if self.output and self.output[-1] != 32:
                    self._put(b" ")
            else:
                if char in b"[{":
                    self.depth += 1
                elif char in b"]}":
                    self.depth -= 1
                if char in b"[{,:":
                    self.nodes += 1
                if not 0 <= self.depth <= 32 or self.nodes > 2048:
                    raise ValueError("envelope_structure_limit")
                self._put(bytes((char,)))

    def finish(self):
        self.decoder.decode(b"", final=True)
        if self.string or self.depth:
            raise ValueError("incomplete_envelope")
        return _loads(self.output)


def _recent_input_fresh(path, identifier, updated):
    """Confirm no later ordinary owner input in a bounded exact-chat tail.

    None means the tail was incomplete, too old, malformed or too large to
    establish freshness. An async-question reply remains in the same task
    phase. This check only gates an explicit local snapshot while the main
    incremental parser is behind or lacks a native turn boundary.
    """
    def inspect(source, start, stop):
        size = stop - start
        source.seek(start)
        if size <= MAX_LINE:
            value = _loads(source.read(size))
        else:
            envelope = _RecordEnvelope(MAX_LINE)
            left = size
            while left:
                chunk = source.read(min(left, 65536))
                if not chunk:
                    raise ValueError("incomplete_tail_record")
                envelope.feed(chunk)
                left -= len(chunk)
            value = envelope.finish()
        if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
            raise ValueError("invalid_tail_record")
        stamp = _time(value.get("timestamp"))
        if stamp is None:
            raise ValueError("invalid_tail_timestamp")
        payload = value["payload"]
        if payload.get("thread_id", identifier) != identifier:
            raise ValueError("wrong_tail_thread")
        if value.get("type") == "session_meta" and payload.get("id") != identifier:
            raise ValueError("wrong_tail_session")
        if value.get("type") == "event_msg" and payload.get("type") == "task_started":
            if not isinstance(payload.get("turn_id"), str) or _time(payload.get("started_at")) is None:
                raise ValueError("invalid_tail_turn")
        item = payload
        if value.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item")
        user = (value.get("type") == "response_item" and payload.get("type") == "message"
                and payload.get("role") == "user") or (value.get("type") == "event_msg"
                and payload.get("type") == "item_completed" and isinstance(item, dict)
                and item.get("type") == "UserMessage")
        if not user:
            return stamp, False
        content = item.get("content") if isinstance(item, dict) else None
        body = "\n".join(part.get("text", "") for part in content if isinstance(part, dict)
                         and isinstance(part.get("text"), str)) if isinstance(content, list) else ""
        technical_reply = (body.startswith("<send_user_message_question_reply>") and
                           body.rstrip().endswith("</send_user_message_question_reply>"))
        return stamp, not technical_reply

    try:
        with path.open("rb") as source:
            end = source.seek(0, 2)
            if end:
                source.seek(end - 1)
                if source.read(1) != b"\n":
                    return None
            lower_bound = max(0, end - MAX_RECENT_INPUT_SCAN)
            position, line_end, previous = end, end, None
            while position > 0:
                block_start = max(0, position - 65536)
                source.seek(block_start)
                block = source.read(position - block_start)
                index = len(block)
                while True:
                    index = block.rfind(b"\n", 0, index)
                    if index < 0:
                        break
                    boundary = block_start + index + 1
                    if boundary >= line_end:
                        continue
                    if boundary < lower_bound:
                        return None
                    stamp, ordinary_user = inspect(source, boundary, line_end)
                    if previous is not None and stamp > previous:
                        return None  # Native append timestamps must not run backward.
                    previous = stamp
                    if stamp >= updated and ordinary_user:
                        return False
                    if stamp < updated:
                        return True
                    line_end = boundary
                position = block_start
            if line_end:
                if lower_bound:
                    return None
                stamp, ordinary_user = inspect(source, 0, line_end)
                if previous is not None and stamp > previous:
                    return None
                if stamp >= updated and ordinary_user:
                    return False
            return True
    except (OSError, ValueError, TypeError, RecursionError):
        return None


class AllTaskProgress:
    def __init__(self, metadata_provider=None, legacy_provider=None, projector=None, *, codex_root=None,
                 cache_size=32, initial_bytes=MAX_INITIAL, append_bytes=MAX_APPEND,
                 line_bytes=MAX_LINE, discovery_limit=256):
        for value, maximum in ((cache_size, 32), (initial_bytes, MAX_INITIAL),
                               (append_bytes, MAX_APPEND), (line_bytes, MAX_LINE), (discovery_limit, 512)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("invalid_budget")
        if append_bytes < 128:
            raise ValueError("append_budget_below_identity_anchor")
        self.codex_root = (Path(codex_root) if codex_root is not None else config.codex_home()).resolve()
        self.metadata_provider, self.legacy_provider, self.projector = metadata_provider, legacy_provider, projector
        self.cache_size, self.initial_bytes, self.append_bytes, self.line_bytes = cache_size, initial_bytes, append_bytes, line_bytes
        self.discovery_limit = discovery_limit
        self.discovery_truncated = False
        self.discovery_complete = False
        self._last_metadata_complete = False
        self._cache, self._trusted = OrderedDict(), {}
        self._bytes_read, self._skipped = 0, 0
        self._lock = threading.RLock()

    def _metadata(self, identifier=None):
        self._last_metadata_complete = False
        if self.metadata_provider is not None:
            rows = self.metadata_provider()
            bounded = list(islice(rows, 4097))
            self._last_metadata_complete = len(bounded) <= 4096
            return [r for r in bounded if isinstance(r, dict) and (identifier is None or r.get("id") == identifier)]
        databases = sorted(self.codex_root.glob("state_*.sqlite"),
                           key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1)
        if not databases:
            return []
        with closing(sqlite3.connect(databases[-1].as_uri() + "?mode=ro", uri=True, timeout=.5)) as db:
            db.execute("PRAGMA query_only=ON")
            columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
            fields = [k for k in ("id", "rollout_path", "cwd", "title", "thread_source", "source", "archived",
                                  "agent_path", "agent_role", "updated_at", "updated_at_ms") if k in columns]
            if not {"id", "rollout_path", "cwd", "thread_source", "archived"}.issubset(fields):
                return []
            query = "SELECT " + ",".join(fields) + " FROM threads WHERE archived=0"
            values = []
            if identifier is not None:
                query += " AND id=?"; values.append(identifier)
            query += " ORDER BY " + ("updated_at_ms" if "updated_at_ms" in columns else "updated_at") + " DESC LIMIT 4097"
            result = [dict(zip(fields, row)) for row in db.execute(query, values)]
            self._last_metadata_complete = len(result) <= 4096
            return result

    def _public(self, meta, trusted=None):
        identifier = meta.get("id")
        return (isinstance(identifier, str) and UUID.fullmatch(identifier) and identifier not in EXCLUDED
                and not meta.get("archived") and meta.get("thread_source") in PUBLIC and not _child(meta)
                and (meta.get("thread_source") != "agent_created_thread" or _native_public(trusted, identifier)))

    def discover(self, rows=None, now=None):
        """Metadata only. Caller must require_thread_scope for each ID before snapshot."""
        with self._lock:
            self.discovery_complete, self.discovery_truncated = False, False
            if rows is not None:
                self._trusted = {r["id"]: dict(r) for r in islice(rows, 4096) if isinstance(r, dict)
                                 and isinstance(r.get("id"), str) and UUID.fullmatch(r["id"])}
            try:
                source = self._metadata()
            except (OSError, ValueError, TypeError, sqlite3.Error):
                return []
            result = []
            for meta in source:
                trusted = self._trusted.get(meta.get("id"), {})
                if not self._public(meta, trusted):
                    continue
                stamp = meta.get("updated_at_ms")
                stamp = stamp // 1000 if type(stamp) is int else meta.get("updated_at", 0)
                result.append({"id": meta["id"], "kind": "codex", "hostId": "local",
                               "title": _safe(meta.get("title") or trusted.get("title"), 160),
                               "cwd": meta.get("cwd"), "status": trusted.get("status", "unknown"),
                               "updatedAt": stamp, "native_verification": trusted.get("native_verification", "local_codex_database")})
            self.discovery_truncated = len(result) > self.discovery_limit or len(source) > 4096
            self.discovery_complete = self._last_metadata_complete and not self.discovery_truncated
            return result[:self.discovery_limit]

    def coverage(self):
        with self._lock:
            return {"cache_entries": len(self._cache), "bytes_read": self._bytes_read,
                    "skipped_lines": self._skipped, "discovery_truncated": self.discovery_truncated,
                    "discovery_complete": self.discovery_complete}

    def catalog(self, rows=None, now=None):
        # Convenience only; gateway should enforce per-ID authorization before calling snapshot itself.
        return [self.snapshot(row["id"], row=row, now=now) for row in self.discover(rows, now)]

    def _clear_turn(self, state, turn, stamp):
        state.update(turn_id=turn, started=stamp, finished=None, runtime="active", plan=None,
                     checks=[], current="", last_report=None, last_public_offset=None)

    def _text(self, state, text, stamp, offset, phase):
        if not state.get("turn_id") or not isinstance(text, str):
            return
        text = _public_lines(text)
        if phase == "commentary":
            state["last_public_offset"] = offset
            state["current"] = _safe(next((line for line in text.splitlines() if line.strip()), ""))
            found = None
            lines = text.splitlines()
            for index, line in enumerate(lines[:-1]):
                if not (line.lstrip().startswith("|") and "Задача" in line and "Статус" in line and "Осталось" in line):
                    continue
                rows = []
                for raw in lines[index + 2:]:
                    if not raw.lstrip().startswith("|"):
                        break
                    cells = [v.strip().strip("*") for v in raw.strip().strip("|").split("|")]
                    if len(cells) < 5 or cells[1] not in {"Готово", "В работе", "Далее", "Ожидание"}:
                        rows = []; break
                    rows.append({"id": "step-" + str(len(rows) + 1), "title": _safe(cells[0], 300),
                                 "done": cells[1] == "Готово",
                                 "status": {"Готово":"completed", "В работе":"in_progress", "Далее":"pending", "Ожидание":"waiting"}[cells[1]],
                                 "percent": 100 if cells[1] == "Готово" else 0})
                    if len(rows) > 64:
                        rows = []; break
                if rows:
                    found = {"done": sum(r["done"] for r in rows), "total": len(rows), "checkpoints": rows,
                             "basis": "assistant_table", "at": stamp, "offset": offset}
            phrases = list(_PHRASE.finditer(text))
            if phrases:
                done, total = map(int, phrases[-1].groups())
                if 0 <= done <= total <= 64 and total:
                    if found and (found["done"], found["total"]) != (done, total):
                        found = None; state["plan"] = None
                    elif not found:
                        found = {"done": done, "total": total, "checkpoints": [],
                                 "basis": "assistant_phrase", "at": stamp, "offset": offset}
            if found:
                state["plan"], state["last_report"] = found, stamp
        for line in text.splitlines():
            if re.search(r"провер|тест|\bPASS\b|\bFAIL\b|\bERROR\b|\bSKIP\b", line, re.I) and re.search(r"\b(?:PASS|FAIL|ERROR|SKIP)\b", line):
                item = {"title": "Сообщено агентом", "status": "reported", "detail": _safe(line),
                        "provenance": "agent_report", "source_at_msk": stamp.isoformat(), "source_byte_offset": offset}
                if item["detail"] and not any(r["detail"] == item["detail"] for r in state["checks"]):
                    state["checks"] = (state["checks"] + [item])[-6:]

    def _record(self, state, value, offset):
        payload = value.get("payload")
        stamp = _time(value.get("timestamp"))
        if not isinstance(payload, dict) or stamp is None:
            return
        state["last_event"] = stamp
        kind, event = value.get("type"), payload.get("type")
        if kind == "response_item" and event == "message" and payload.get("role") == "user":
            parts = payload.get("content")
            body = "\n".join(part.get("text", "") for part in parts if isinstance(part, dict)
                             and isinstance(part.get("text"), str)) if isinstance(parts, list) else ""
            question_reply = (body.startswith("<send_user_message_question_reply>") and
                              body.rstrip().endswith("</send_user_message_question_reply>"))
            if not question_reply:
                state["last_user"] = stamp
                state["last_user_offset"] = offset
        if kind == "event_msg" and event == "task_started":
            turn = payload.get("turn_id")
            if isinstance(turn, str) and 0 < len(turn) <= 150:
                self._clear_turn(state, turn, _time(payload.get("started_at")) or stamp)
            return
        if kind == "turn_context":
            turn = payload.get("turn_id")
            if isinstance(turn, str) and 0 < len(turn) <= 150 and turn != state.get("turn_id"):
                self._clear_turn(state, turn, stamp)
            return
        if not state.get("turn_id"):
            return
        if kind == "event_msg" and event in {"task_complete", "turn_aborted"}:
            if payload.get("turn_id", state["turn_id"]) == state["turn_id"]:
                state["runtime"] = "completed" if event == "task_complete" else "interrupted"
                state["finished"] = _time(payload.get("completed_at")) or stamp
            return
        if kind == "response_item" and event == "function_call" and str(payload.get("name", "")).split(".")[-1] == "update_plan":
            args = payload.get("arguments")
            try:
                args = _loads(args) if isinstance(args, str) else args
            except ValueError:
                return
            steps = args.get("plan") if isinstance(args, dict) else None
            if (isinstance(steps, list) and 1 <= len(steps) <= 64 and
                    all(isinstance(s, dict) and s.get("status") in {"pending", "in_progress", "completed"}
                        and isinstance(s.get("step"), str) for s in steps)):
                points = [{"id": "step-" + str(i + 1), "title": _safe(s["step"], 300), "done": s["status"] == "completed",
                           "status": s["status"], "percent": 100 if s["status"] == "completed" else 0}
                          for i, s in enumerate(steps)]
                state["plan"] = {"done": sum(p["done"] for p in points), "total": len(points), "checkpoints": points,
                                 "basis": "update_plan", "at": stamp, "offset": offset}
                state["last_report"] = stamp
            return
        item = payload
        if kind == "event_msg" and event == "item_completed":
            if payload.get("turn_id") != state["turn_id"]:
                return
            item = payload.get("item", {})
            allowed = isinstance(item, dict) and item.get("type") == "AgentMessage"
        else:
            allowed = kind == "response_item" and event == "message" and item.get("role") == "assistant"
        if allowed and item.get("phase") in {"commentary", "final", "final_answer"}:
            content = item.get("content", [])
            if isinstance(content, list):
                text = "\n".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)
                                 and p.get("type") in {"text", "Text", "output_text"})
                self._text(state, text, stamp, offset, item["phase"])

    def _large_record(self, state, envelope, offset):
        value = envelope.finish()
        if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
            raise ValueError("invalid_envelope")
        payload, stamp = value["payload"], _time(value.get("timestamp"))
        if stamp is None:
            raise ValueError("invalid_envelope_time")
        kind, event = value.get("type"), payload.get("type")
        if kind == "response_item" and event in {"message", "function_call_output", "custom_tool_call_output", "reasoning"}:
            # Attachments/output are not turn boundaries or sources of plans.
            state["last_event"] = stamp
            if event == "message" and payload.get("role") == "user":
                state["last_user"] = stamp
                state["last_user_offset"] = offset
        elif kind == "turn_context" or kind == "event_msg" and event in {"task_started", "task_complete", "turn_aborted"}:
            if not isinstance(payload.get("turn_id"), str) or not 0 < len(payload["turn_id"]) <= 150:
                raise ValueError("invalid_envelope_turn")
            self._record(state, value, offset)
        else:
            raise ValueError("unknown_large_record")

    def _feed(self, state, chunk):
        start = state["offset"] - len(state["buffer"])
        data = state["buffer"] + chunk
        state["offset"] += len(chunk)
        if chunk:
            state["anchor"] = (state.get("anchor", b"") + chunk)[-64:]
        if state["discard"]:
            end = data.find(b"\n")
            if end < 0:
                state["buffer"] = b""; return
            start += end + 1; data = data[end + 1:]; state["discard"] = False
        if state.get("envelope") is not None:
            end = data.find(b"\n")
            piece = data if end < 0 else data[:end]
            try:
                state["envelope"].feed(piece)
                if end >= 0:
                    self._large_record(state, state["envelope"], state["envelope_start"])
            except (ValueError, UnicodeError, RecursionError):
                self._clear_turn(state, None, None); state["runtime"] = "unknown"
                state["envelope"] = None
                if end < 0:
                    state["discard"] = True
            if end < 0:
                state["buffer"] = b""; return
            state["envelope"] = None
            start += end + 1; data = data[end + 1:]
        parts = data.split(b"\n")
        state["buffer"] = parts.pop()
        for line in parts:
            if len(line) > self.line_bytes:
                self._skipped += 1
                try:
                    envelope = _RecordEnvelope(self.line_bytes)
                    envelope.feed(line)
                    self._large_record(state, envelope, start)
                except (ValueError, UnicodeError, RecursionError):
                    self._clear_turn(state, None, None); state["runtime"] = "unknown"
            else:
                try:
                    value = _loads(line)
                    if isinstance(value, dict):
                        self._record(state, value, start)
                except (ValueError, UnicodeError, RecursionError):
                    if line.strip():
                        self._clear_turn(state, None, None); state["runtime"] = "unknown"
            start += len(line) + 1
        if len(state["buffer"]) > self.line_bytes:
            self._skipped += 1
            try:
                state["envelope"] = _RecordEnvelope(self.line_bytes)
                state["envelope_start"] = start
                state["envelope"].feed(state["buffer"])
            except (ValueError, UnicodeError, RecursionError):
                state["envelope"] = None; state["discard"] = True
                self._clear_turn(state, None, None); state["runtime"] = "unknown"
            state["buffer"] = b""

    def _state(self, identifier, path):
        stat = path.stat(); identity = (stat.st_dev, stat.st_ino)
        state = self._cache.get(identifier)
        reset = (state is None or state["path"] != path or state["identity"] != identity or stat.st_size < state["offset"]
                 or (stat.st_size == state["size"] and stat.st_mtime_ns != state["mtime"]))
        if not reset and stat.st_size == state["offset"] and stat.st_mtime_ns == state["mtime"]:
            self._cache.move_to_end(identifier); return state
        with path.open("rb") as handle:
            budget = self.append_bytes
            if not reset:
                anchor = state["anchor"]
                handle.seek(state["offset"] - len(anchor))
                actual = handle.read(min(len(anchor), budget)); self._bytes_read += len(actual); budget -= len(actual)
                reset = actual != anchor
            if reset:
                handle.seek(0); line = handle.readline(self.line_bytes + 1); self._bytes_read += len(line)
                header = _loads(line)
                meta = header.get("payload", {})
                if (not line.endswith(b"\n") or len(line) > self.line_bytes or header.get("type") != "session_meta"
                        or not isinstance(meta, dict) or meta.get("id") != identifier or _child(meta)):
                    raise ValueError("invalid_session_identity")
                start = max(0, stat.st_size - self.initial_bytes)
                state = {"path": path, "identity": identity, "offset": start, "buffer": b"", "discard": bool(start),
                         "anchor": b"", "turn_id": None, "started": None, "finished": None, "runtime": "unknown",
                         "plan": None, "checks": [], "current": "", "last_report": None, "last_event": None,
                         "last_user": None, "last_user_offset": None, "last_public_offset": None}
                budget = self.initial_bytes
            handle.seek(state["offset"]); chunk = handle.read(budget); self._bytes_read += len(chunk)
            self._feed(state, chunk)
        state.update(size=stat.st_size, mtime=stat.st_mtime_ns,
                     caught_up=state["offset"] >= stat.st_size and not state["buffer"]
                     and not state["discard"] and state.get("envelope") is None)
        self._cache[identifier] = state; self._cache.move_to_end(identifier)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return state

    def _explicit(self, identifier, meta, state, checked, *, native_caught_up=True,
                  new_input_pending=False):
        anchored = native_caught_up and not new_input_pending and state.get("started") is not None
        candidates = []
        try:
            cwd = _normal_path(meta.get("cwd"), trusted_cwd=True)
            path = cwd / ".task-progress" / (identifier + ".json")
            if _normal_path(path).is_relative_to(cwd):
                with path.open("rb") as handle:
                    data = handle.read(MAX_JSON + 1)
                if len(data) <= MAX_JSON:
                    candidates.append((_loads(data), "workspace_protocol"))
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        if anchored and self.legacy_provider is not None:
            try:
                candidates.append((self.legacy_provider(identifier), "legacy_protocol"))
            except (OSError, ValueError, TypeError):
                pass
        for raw, basis in candidates:
            if not isinstance(raw, dict):
                continue
            if raw.get("schema") == "codex-task-progress/1" and raw.get("available") is True:
                raw = {"task_id": raw.get("thread_id"), "task": raw.get("title"), "overall_status": raw.get("status"),
                       "current": raw.get("current"), "forecast": raw.get("forecast", ""),
                       "started_at": raw.get("started_at_msk"), "updated_at": raw.get("updated_at_msk"),
                       "finished_at": raw.get("finished_at_msk"), "metrics": raw.get("metrics", {}),
                       "checkpoints": [dict({"id": p.get("id"), "label": p.get("title"), "done": p.get("done")},
                                            **{k:p[k] for k in ("status","percent","count_done","count_total","unit") if k in p})
                                       for p in raw.get("checkpoints", [])],
                       "lanes": raw.get("results", [])}
            updated, finished = _time(raw.get("updated_at")), _time(raw.get("finished_at"))
            if updated is None:
                continue
            if state.get("last_user") is not None and updated < state["last_user"]:
                continue
            if anchored and (updated < state["started"] or
                             (finished is not None and finished < state["started"])):
                continue
            # A cold tail may omit the native turn boundary after a long active
            # task. Only the exact workspace protocol may supply an unanchored
            # snapshot. Its bounded reverse tail must also prove that a newer
            # owner request is not hidden behind the incremental parser.
            if not anchored and basis != "workspace_protocol":
                continue
            if not anchored and _recent_input_fresh(state["path"], identifier, updated) is not True:
                continue
            try:
                projector = self.projector
                if projector is None:
                    from task_progress import project
                    projector = project
                value = projector(raw, identifier, now=checked)
                if value.get("available") is True and value.get("thread_id") == identifier:
                    value.update(source=basis, progress_basis=basis if anchored else "workspace_protocol_unconfirmed_turn")
                    if not anchored:
                        value.update(stale=True, running=False,
                                     status="Локальный срез · текущий ход не подтверждён")
                    value["results"] = [dict(item, provenance="agent_report", source_at_msk=updated.isoformat())
                                        for item in value.get("results", [])]
                    return value
            except (ValueError, TypeError, KeyError, RecursionError):
                continue
        return None

    def snapshot(self, thread_id, row=None, now=None):
        checked = now or dt.datetime.now(MSK)
        if not isinstance(checked, dt.datetime) or checked.tzinfo is None:
            raise ValueError("aware_clock_required")
        checked = checked.astimezone(MSK)
        unavailable = {"schema": "codex-task-progress/1", "available": False, "status": "unavailable",
                       "checked_at_msk": checked.isoformat(), "reason": "metadata_or_source_unavailable"}
        if not isinstance(thread_id, str) or not UUID.fullmatch(thread_id):
            return unavailable
        with self._lock:
            try:
                records = self._metadata(thread_id)
                meta = next((m for m in records if m.get("id") == thread_id), None)
                trusted = row if row is not None else self._trusted.get(thread_id)
                if meta is None or not self._public(meta, trusted):
                    return unavailable
                path = _normal_path(meta.get("rollout_path"), trusted_rollout=True)
                if not path.is_relative_to(self.codex_root / "sessions") or path.suffix != ".jsonl" or not path.stem.endswith(thread_id):
                    return unavailable
                state = self._state(thread_id, path)
                caught_up = state.get("caught_up", True)
                last_user_offset = state.get("last_user_offset")
                plan_offset = state["plan"]["offset"] if state.get("plan") else None
                new_input_pending = (last_user_offset is not None and
                                     ((plan_offset is not None and last_user_offset > plan_offset)
                                      or (state.get("last_public_offset") is not None
                                          and last_user_offset > state["last_public_offset"])))
                runtime = state["runtime"] if caught_up and not new_input_pending else "unknown"
                age = max(0, int((checked - (state["last_event"] or checked)).total_seconds()))
                stale = (not caught_up or new_input_pending or state["turn_id"] is None
                         or runtime == "active" and age > 180)
                # An active rollout may append faster than the bounded native
                # parser catches up. The exact workspace snapshot is still
                # useful, but cannot be attributed to the current native turn.
                value = self._explicit(thread_id, meta, state, checked,
                                       native_caught_up=caught_up,
                                       new_input_pending=new_input_pending)
                if value is None:
                    plan = state["plan"] if caught_up and not new_input_pending else None
                    if plan and state.get("last_user") is not None and plan["at"] < state["last_user"]:
                        plan = None
                    stamp = (plan["at"] if plan else state["last_event"]) or checked
                    value = {"schema": "codex-task-progress/1", "available": True, "thread_id": thread_id,
                             "title": _safe(meta.get("title"), 160) or "Задача Codex",
                             "status": {"active": "В работе", "completed": "Ответ завершён", "interrupted": "Прервано"}.get(runtime, "Статус текущего хода неизвестен"),
                             "current": state["current"] if caught_up and not new_input_pending else "", "forecast": "",
                             "started_at_msk": state["started"].isoformat() if caught_up and not new_input_pending and state["started"] else None,
                             "updated_at_msk": stamp.isoformat(), "checked_at_msk": checked.isoformat(),
                             "finished_at_msk": state["finished"].isoformat() if caught_up and not new_input_pending and state["finished"] else None,
                             "final": runtime in {"completed", "interrupted"}, "stale": stale,
                             "done": plan["done"] if plan else None, "total": plan["total"] if plan else None,
                             "percent": math.floor(plan["done"] / plan["total"] * 100 + .5) if plan else None,
                             "checkpoints": plan["checkpoints"] if plan else [], "results": list(state["checks"]) if caught_up else [], "metrics": {},
                             "source": "native_rollout", "progress_basis": plan["basis"] if plan else "unknown"}
                value.update(runtime_status=runtime, runtime_stale=stale, runtime_checked_at_msk=checked.isoformat(),
                             turn_confirmed=caught_up and not new_input_pending and bool(state["turn_id"]),
                             running=runtime == "active" and not stale)
                if runtime == "interrupted":
                    value.update(percent=None, final=True, status="Прервано")
                return value
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError):
                self._cache.pop(thread_id, None)
                return unavailable

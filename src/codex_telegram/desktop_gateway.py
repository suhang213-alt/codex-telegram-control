"""Transparent transport for the engine launched by the verified Desktop app.

No engine initialization or second app-server is performed here. The local
broker adds only scoped, owner-authorized operations to that existing stream.
"""
from __future__ import annotations
import argparse
from collections import deque
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from .config import SafeError, atomic_json, load

MAX_FRAME = 8 * 1024 * 1024
MAX_EVENT = 512 * 1024
MAX_EVENTS_BYTES = 16 * 1024 * 1024
APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
USER_INPUT = "item/tool/requestUserInput"
MUTATIONS = {"thread/resume", "turn/start", "turn/steer", "turn/interrupt"}
RPC_METHODS = MUTATIONS | {"thread/list", "thread/read", "model/list"}
EVENT_METHODS = {"thread/started", "turn/started", "turn/completed", "item/started", "item/completed",
                 "item/agentMessage/delta", "error", "serverRequest/resolved", "gateway/requestClaimed"}
SECRET = re.compile(r"password|passwd|secret|credential|api.?key|access.?token|refresh.?token", re.I)


def wire(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def typed(identifier):
    if type(identifier) not in (str, int) or isinstance(identifier, str) and not identifier:
        raise SafeError("Invalid native request identity.")
    return (type(identifier).__name__, identifier)


def decode(line):
    if len(line) > MAX_FRAME:
        return None
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        return None


def write_all(stream, data):
    """Blocking raw pipes may accept only part of a buffer."""
    remaining = memoryview(data)
    while remaining:
        count = stream.write(remaining)
        if count is None or count <= 0 or count > len(remaining):
            raise OSError("Native stream write did not complete.")
        remaining = remaining[count:]
    stream.flush()


class _RoutingFields:
    """Bounded lexical extraction of root id/method from native JSON frames.

    This does not interpret payload values or validate the application protocol.
    Only short root keys and a string id are retained; nested content is skipped.
    """
    special = re.compile(rb'["\\]')

    def __init__(self):
        self.depth = 0
        self.started = False
        self.root_object = False
        self.expect = "key"
        self.key = None
        self.string = False
        self.escape = False
        self.role = None
        self.capture = None
        self.identifier = None
        self.has_method = False

    def _append(self, data):
        if self.capture is not None:
            if len(self.capture) + len(data) > 512: self.capture = None
            else: self.capture.extend(data)

    def feed(self, data):
        position = 0
        while position < len(data):
            if self.string:
                if self.escape:
                    self._append(data[position:position+1])
                    self.escape = False; position += 1
                    continue
                match = self.special.search(data, position)
                end = match.start() if match else len(data)
                self._append(data[position:end])
                if match is None: return
                value = data[end]; self._append(data[end:end+1]); position = end + 1
                if value == 92:
                    self.escape = True
                    continue
                self.string = False
                decoded = None
                if self.capture is not None:
                    try: decoded = json.loads(self.capture)
                    except (ValueError, UnicodeError): pass
                if self.role == "key":
                    self.key = decoded; self.expect = "colon"
                    if decoded == "method": self.has_method = True
                elif self.role == "value":
                    if self.key == "id": self.identifier = decoded
                    self.expect = "after"
                self.capture = None; self.role = None
                continue
            value = data[position]; position += 1
            if value in b" \r\n\t": continue
            if not self.started:
                self.started = True; self.root_object = value == 123
            if value == 34:
                self.string = True
                self.role = self.expect if self.depth == 1 and self.expect in {"key", "value"} else None
                self.capture = bytearray(b'"') if self.role == "key" or self.role == "value" and self.key == "id" else None
                if self.role == "value" and self.key == "id": self.identifier = None
            elif value in (123, 91):
                if self.depth == 1 and self.expect == "value":
                    if self.key == "id": self.identifier = None
                    self.expect = "after"
                self.depth += 1
            elif value in (125, 93): self.depth -= 1
            elif self.depth == 1:
                if value == 58 and self.expect == "colon": self.expect = "value"
                elif value == 44: self.expect = "key"; self.key = None
                elif self.expect == "value":
                    if self.key == "id": self.identifier = None
                    self.expect = "after"


class GatewayCore:
    """Testable broker state; writers must flush exact bytes to their streams."""
    def __init__(self, config, write_engine, write_gui, *, persist=lambda value: None,
                 operations=None, epoch=None, rpc_timeout=30):
        self.workspace = Path(config["workspace"]).resolve()
        self.write_engine, self.write_gui, self.persist = write_engine, write_gui, persist
        self.epoch = epoch or secrets.token_hex(16)
        self.rpc_timeout = rpc_timeout
        self.on_status = lambda: None
        self.compatibility_check = lambda: True
        self.last_compatible = True
        self.lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.connected = True
        self.initialized = False
        self.initialize_ok = False
        self.rpc_counter = 0
        self.rpc_pending = {}
        self.gui_requests = {}
        self.gui_frame_incomplete = False
        self.engine_frame_incomplete = False
        self.engine_spool = None
        self.engine_routing = None
        self.scopes = {}
        self.active = {}
        self.pending = {}
        self.contexts = {}
        self.events = deque()
        self.events_bytes = 0
        self.cursor = 0
        self.operations = dict(operations or {})
        for value in self.operations.values():
            if value.get("state") == "dispatching": value["state"] = "unknown"
        self._persist()

    @property
    def ready(self):
        return self.connected and self.initialized and self.initialize_ok and self.compatibility_check()

    def _persist(self):
        self.persist({"schema": "desktop-gateway-operations/1", "operations": self.operations})

    def _allowed_path(self, value):
        if not isinstance(value, str) or not Path(value).is_absolute(): return False
        try:
            return Path(value).resolve().is_relative_to(self.workspace)
        except (OSError, ValueError):
            return False

    def _metadata(self, row):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str): return
        self.scopes[row["id"]] = self._allowed_path(row.get("cwd"))

    def _observed_result(self, method, result):
        if not isinstance(result, dict): return
        if method == "thread/list":
            for row in result.get("data", []): self._metadata(row)
        elif method in {"thread/read", "thread/resume", "thread/start"}:
            self._metadata(result.get("thread"))

    def _scoped(self, identifier):
        return isinstance(identifier, str) and self.scopes.get(identifier) is True

    def _event_thread(self, event):
        params = event.get("params", {})
        if not isinstance(params, dict): return None
        thread = params.get("thread")
        return params.get("threadId") or (thread.get("id") if isinstance(thread,dict) else None)

    def _publish(self, event):
        if not self._scoped(self._event_thread(event)): return
        self.cursor += 1
        size = len(wire(event))
        if size <= MAX_EVENT:
            self.events.append((self.cursor, event, size))
            self.events_bytes += size
        else:
            # A skipped frame creates a cursor gap; clients must reconcile.
            self.events.clear(); self.events_bytes = 0
        while len(self.events) > 1000 or self.events_bytes > MAX_EVENTS_BYTES:
            self.events_bytes -= self.events.popleft()[2]
        self.condition.notify_all()

    def _claim(self, row, by):
        row["state"] = by + "_submitted"
        params = row["event"]["params"]
        self._publish({"method": "gateway/requestClaimed", "params": {
            "requestId": row["event"]["id"], "threadId": params["threadId"],
            "turnId": params.get("turnId"), "by": by}})

    def from_gui(self, line):
        with self.lock:
            before = self.ready
            continuation = self.gui_frame_incomplete
            self.gui_frame_incomplete = not line.endswith(b"\n")
            # Oversized GUI frames stream unchanged. Until their newline arrives,
            # no broker request may be inserted into this same engine byte stream.
            try: suppress = False if continuation or self.gui_frame_incomplete else self._observe_gui(line)
            except (ValueError,TypeError,AttributeError,SafeError): suppress = False
            if not suppress: self.write_engine(line)
            if before != self.ready:
                try: self.on_status()
                except Exception: pass  # Metadata must not break Desktop transport.

    def _observe_gui(self, line):
        value = decode(line)
        with self.lock:
            if value is not None:
                if "method" not in value and "id" in value:
                    try: row = self.pending.get(typed(value["id"]))
                    except SafeError: row = None
                    if row is not None:
                        if row["state"] != "pending": return True
                        self._claim(row, "desktop")
                method = value.get("method")
                if method == "initialized": self.initialized = True
                if "id" in value and method in {"initialize", "thread/list", "thread/read", "thread/resume", "thread/start"}:
                    try:
                        if len(self.gui_requests) < 4096:
                            self.gui_requests[typed(value["id"])] = method
                    except SafeError: pass
            return False

    def _safe_request(self, value):
        method, params = value.get("method"), value.get("params")
        if method not in APPROVALS | {USER_INPUT} or not isinstance(params, dict): return False
        if not isinstance(params.get("threadId"), str) or not isinstance(params.get("turnId"), str): return False
        if method == USER_INPUT:
            questions = params.get("questions")
            if not isinstance(questions, list) or not 1 <= len(questions) <= 20: return False
            for q in questions:
                if not isinstance(q, dict) or q.get("isSecret") or SECRET.search(str(q.get("id", "")) + " " + str(q.get("question", ""))): return False
        return len(wire(value)) <= 200000

    def from_engine(self, line):
        with self.lock:
            before = self.ready
            self.engine_frame_incomplete = not line.endswith(b"\n")
            if self.engine_spool is not None or self.engine_frame_incomplete or len(line) > MAX_FRAME:
                # A root id can follow a huge result. Do not expose even the first
                # byte until routing is known. TemporaryFile is private (0600 on
                # POSIX), anonymous/unlinked and never recorded in logs/manifests.
                if self.engine_spool is None:
                    self.engine_spool = tempfile.TemporaryFile(mode="w+b")
                    self.engine_routing = _RoutingFields()
                try:
                    write_all(self.engine_spool, line)
                    self.engine_routing.feed(line)
                    if self.engine_frame_incomplete: return
                    self.engine_spool.seek(0)
                    fields = self.engine_routing
                    identifier = fields.identifier
                    own = (fields.root_object and not fields.has_method and isinstance(identifier, str)
                           and identifier.startswith("ctd-" + self.epoch + "-"))
                    if own:
                        target = self.rpc_pending.get(identifier)
                        if target is not None:
                            try: target.put_nowait({"error":{"code":"response_too_large"}})
                            except queue.Full: pass
                    else:
                        while chunk := self.engine_spool.read(MAX_FRAME): self.write_gui(chunk)
                finally:
                    if not self.engine_frame_incomplete:
                        self.engine_spool.close(); self.engine_spool = None; self.engine_routing = None
            else:
                try: suppress = self._observe_engine(line)
                except (ValueError,TypeError,AttributeError,SafeError): suppress = False
                if not suppress: self.write_gui(line)
            if before != self.ready:
                try: self.on_status()
                except Exception: pass

    def _observe_engine(self, line):
        value = decode(line)
        with self.lock:
            if value is not None:
                if "method" not in value and "id" in value:
                    identifier = value["id"]
                    if isinstance(identifier, str) and identifier.startswith("ctd-" + self.epoch + "-"):
                        target = self.rpc_pending.get(identifier)
                        if target is not None:
                            try: target.put_nowait(value)
                            except queue.Full: pass
                        return True
                    try: method = self.gui_requests.pop(typed(identifier), None)
                    except SafeError: method = None
                    if "error" not in value:
                        if method == "initialize": self.initialize_ok = True
                        self._observed_result(method, value.get("result"))
                method, params = value.get("method"), value.get("params")
                if isinstance(params, dict):
                    identifier = self._event_thread(value)
                    if method == "thread/started": self._metadata(params.get("thread"))
                    if method == "turn/started":
                        turn = params.get("turn") or {}
                        if isinstance(turn.get("id"), str): self.active[identifier] = turn["id"]
                    elif method == "turn/completed":
                        if self.active.get(identifier) == (params.get("turn") or {}).get("id"):
                            self.active.pop(identifier, None)
                        self.contexts = {k:v for k,v in self.contexts.items() if k[0] != identifier}
                    if method in {"item/started", "item/completed"}:
                        item = params.get("item") or {}
                        if item.get("type") in {"commandExecution", "fileChange"} and isinstance(item.get("id"), str):
                            if len(wire(value)) <= 200000:
                                key = (identifier, params.get("turnId"), item["id"])
                                self.contexts[key] = {"method":"item/started", "params":params}
                                while len(self.contexts) > 500: self.contexts.pop(next(iter(self.contexts)))
                    if method == "serverRequest/resolved":
                        try: key = typed(params.get("requestId"))
                        except SafeError: key = None
                        row = self.pending.get(key)
                        if row and row["event"]["params"].get("threadId") == identifier:
                            row["state"] = "resolved"
                    if "id" in value and self._safe_request(value):
                        try:
                            key = typed(value["id"])
                            prior = self.pending.get(key)
                            if prior is None or prior["state"] == "resolved":
                                if len(self.pending) >= 1000:
                                    self.pending = {k:v for k,v in self.pending.items() if v["state"] != "resolved"}
                                if len(self.pending) < 1000:
                                    self.pending[key] = {"event": value, "state":"pending"}
                                    self._publish(value)
                        except SafeError: pass
                    elif method in EVENT_METHODS:
                        # Hidden reasoning/tool output is not a panel stream.
                        item = params.get("item") or {}
                        if method not in {"item/started", "item/completed"} or item.get("type") in {"agentMessage", "userMessage", "commandExecution", "fileChange"}:
                            self._publish(value)
            return False

    def _request(self, method, params, deadline=None):
        with self.lock:
            if not self.ready: raise SafeError("Desktop engine is not ready. No operation was sent.")
            if self.gui_frame_incomplete: raise SafeError("Desktop input is still arriving. No broker operation was sent.")
            remaining = self.rpc_timeout if deadline is None else deadline - time.monotonic()
            if remaining <= 0: raise SafeError("Native operation deadline expired. Nothing further was sent.")
            self._active_check(method,params)
            self.rpc_counter += 1
            identifier = "ctd-" + self.epoch + "-" + str(self.rpc_counter)
            response = queue.Queue(maxsize=1)
            self.rpc_pending[identifier] = response
            try: self.write_engine(wire({"id":identifier,"method":method,"params":params}))
            except Exception:
                self.rpc_pending.pop(identifier, None)
                raise SafeError("Desktop transport failed; outcome may be unknown.") from None
        try:
            try: value = response.get(timeout=remaining)
            except queue.Empty: raise SafeError("Desktop response timed out; do not automatically resend.") from None
            if "error" in value:
                if isinstance(value["error"], dict) and value["error"].get("code") == "response_too_large":
                    raise SafeError("Desktop response exceeds the 8 MiB broker limit. Review this chat in Desktop.")
                raise SafeError("Desktop rejected the operation. Review Desktop before retrying.")
            result = value.get("result")
            if not isinstance(result, dict): raise SafeError("Unexpected Desktop protocol response.")
            with self.lock: self._observed_result(method, result)
            return result
        finally:
            with self.lock: self.rpc_pending.pop(identifier, None)

    def _refresh_scope(self, identifier, deadline=None):
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
            raise SafeError("A native chat identifier is required.")
        result = self._request("thread/read", {"threadId":identifier,"includeTurns":False},deadline)
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != identifier or not self._allowed_path(thread.get("cwd")):
            with self.lock: self.scopes[identifier] = False
            raise SafeError("Chat is outside the configured workspace.")

    def _validated_rpc(self, method, params):
        if method not in RPC_METHODS or not isinstance(params, dict): raise SafeError("Native operation is not allowed.")
        if method in {"thread/list", "model/list"}:
            allowed = {"limit", "cursor"} | ({"sourceKinds"} if method == "thread/list" else set())
            if set(params) - allowed: raise SafeError("Unsupported list options.")
            if "sourceKinds" in params:
                kinds = params["sourceKinds"]
                if not isinstance(kinds, list) or len(kinds) > 3 or any(not isinstance(kind, str) or kind not in {"cli", "vscode", "appServer"} for kind in kinds):
                    raise SafeError("Unsupported native chat source kinds.")
            if "limit" in params and (type(params["limit"]) is not int or not 1 <= params["limit"] <= 100): raise SafeError("Invalid page limit.")
            if "cursor" in params and params["cursor"] is not None and (not isinstance(params["cursor"], str) or len(params["cursor"]) > 4000): raise SafeError("Invalid page cursor.")
            return
        allowed = {"threadId"}
        if method == "thread/read": allowed |= {"includeTurns"}
        elif method == "turn/start": allowed |= {"input", "model", "effort"}
        elif method == "turn/steer": allowed |= {"input", "expectedTurnId"}
        elif method == "turn/interrupt": allowed |= {"turnId"}
        if set(params) - allowed: raise SafeError("Native settings overrides are not allowed.")
        if "includeTurns" in params and type(params["includeTurns"]) is not bool: raise SafeError("Invalid history option.")
        if method in {"turn/start", "turn/steer"}:
            inputs = params.get("input")
            if not isinstance(inputs, list) or not 1 <= len(inputs) <= 5: raise SafeError("Text input is required.")
            total = 0
            for value in inputs:
                if not isinstance(value, dict) or set(value) != {"type", "text"} or value["type"] != "text" or not isinstance(value["text"], str) or not value["text"]:
                    raise SafeError("Only explicit text input is supported.")
                total += len(value["text"])
            if total > 100000: raise SafeError("Input exceeds the native limit.")
            if "model" in params and (not isinstance(params["model"], str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", params["model"])): raise SafeError("Invalid model.")
            if "effort" in params and params["effort"] not in {"none","minimal","low","medium","high","xhigh","max","ultra"}: raise SafeError("Invalid reasoning effort.")

    def _active_check(self, method, params):
        identifier = params.get("threadId")
        active = self.active.get(identifier)
        if method == "turn/start" and active: raise SafeError("The selected chat is already running; use steering.")
        if method == "turn/steer" and (not active or params.get("expectedTurnId") != active): raise SafeError("Active turn changed; reload the panel.")
        if method == "turn/interrupt" and (not active or params.get("turnId") != active): raise SafeError("Active turn changed; reload the panel.")

    def _journal_begin(self, message):
        identifier = message.get("operation_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", identifier): raise SafeError("A unique operation identifier is required.")
        payload = {k:v for k,v in message.items() if k != "token"}
        digest = hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        prior = self.operations.get(identifier)
        if prior:
            if prior["digest"] != digest: raise SafeError("Operation identifier was reused for different input.")
            if prior["state"] != "completed": raise SafeError("Previous outcome is unknown. Reconcile before retrying.")
            return identifier, prior["result"]
        if len(self.operations) >= 20000: raise SafeError("Native operation journal is full.")
        self.operations[identifier] = {"digest":digest,"state":"dispatching"}
        try: self._persist()
        except Exception:
            self.operations.pop(identifier, None)
            raise SafeError("Native operation could not be recorded. Nothing was sent.") from None
        return identifier, None

    @staticmethod
    def _receipt(method, result):
        if method == "thread/resume": return {"thread":{"id":result.get("thread", {}).get("id")}}
        if method in {"turn/start", "turn/steer"}:
            turn = result.get("turn") or {}
            return {"turnId":result.get("turnId") or turn.get("id"),"turn":{"id":turn.get("id"),"status":turn.get("status")}}
        return {} if method == "turn/interrupt" else {"response_sent":True,"native_confirmation_pending":True}

    def _reply_check(self, message):
        if not self.ready: raise SafeError("Desktop engine compatibility is no longer confirmed.")
        if self.gui_frame_incomplete: raise SafeError("Desktop input is still arriving. No broker response was sent.")
        key = typed(message.get("request_id"))
        row = self.pending.get(key)
        if not row or row["state"] != "pending": raise SafeError("Native request is no longer open.")
        event = row["event"]; params = event["params"]; result = message.get("result")
        if (params["threadId"] != message.get("thread_id") or params["turnId"] != message.get("turn_id")
                or self.active.get(params["threadId"]) != params["turnId"]): raise SafeError("Native request identity or active turn changed.")
        if not isinstance(result, dict): raise SafeError("Invalid native response.")
        if event["method"] in APPROVALS:
            if set(result) != {"decision"} or result["decision"] not in {"accept", "decline", "cancel"}: raise SafeError("Unsupported approval decision.")
            available = params.get("availableDecisions")
            if available is not None and result["decision"] not in available: raise SafeError("Decision is unavailable for this request.")
            if result["decision"] == "accept":
                context = self.contexts.get((params["threadId"],params["turnId"],params.get("itemId")), {}).get("params", {}).get("item", {})
                review = dict(params, contextItem=context)
                if len(wire(review)) > 200000: raise SafeError("Approval context exceeds the review limit.")
                if event["method"] == "item/commandExecution/requestApproval":
                    if not isinstance(params.get("command") or context.get("command"), str) or not (params.get("command") or context.get("command")): raise SafeError("Full command is unavailable; use Desktop.")
                else:
                    changes = context.get("changes")
                    if not isinstance(changes, list) or not changes or not all(isinstance(c,dict) and isinstance(c.get("path"),str) and isinstance(c.get("diff"),str) for c in changes): raise SafeError("Full file changes are unavailable; use Desktop.")
        else:
            questions = params["questions"]
            if set(result) != {"answers"} or not isinstance(result["answers"],dict) or set(result["answers"]) != {q.get("id") for q in questions}: raise SafeError("Answers must match the native questions.")
            for q in questions:
                answer = result["answers"][q["id"]]
                if not isinstance(answer,dict) or set(answer) != {"answers"} or not isinstance(answer["answers"],list) or len(answer["answers"]) != 1: raise SafeError("Invalid native answer.")
                text = answer["answers"][0]
                if not isinstance(text,str) or not 1 <= len(text) <= 8000: raise SafeError("Invalid native answer.")
                options = q.get("options")
                if options and not q.get("isOther") and text not in [o.get("label") for o in options if isinstance(o,dict)]: raise SafeError("Select an available native option.")
        return row

    def status(self):
        with self.lock:
            compatible = self.compatibility_check()
            if compatible != self.last_compatible:
                self.last_compatible = compatible
                try: self.on_status()
                except Exception: pass
            result = {"ready":self.ready,"connected":self.connected,"epoch":self.epoch,"backend":"desktop",
                    "ambiguous":not compatible,
                    "active":{k:v for k,v in self.active.items() if self._scoped(k)},
                    "pending":[r["event"] for r in self.pending.values() if r["state"] == "pending" and self._scoped(r["event"]["params"]["threadId"])],
                    "contexts":[event for key,event in self.contexts.items() if self._scoped(key[0])],"cursor":self.cursor}
            if len(wire(result)) > MAX_FRAME//2: raise SafeError("Native bootstrap exceeds the safe limit. Resolve pending work in Desktop.")
            return result

    def command(self, message):
        if not isinstance(message,dict) or message.get("epoch") != self.epoch: raise SafeError("Native connection changed. Restart the bridge.")
        command = message.get("command")
        if command == "status": return self.status()
        if command == "events":
            after, timeout = message.get("after",0), message.get("timeout",0)
            if type(after) is not int or after < 0 or type(timeout) not in (int,float) or not 0 <= timeout <= 5: raise SafeError("Invalid event cursor or timeout.")
            with self.condition:
                if after == self.cursor and self.connected: self.condition.wait(timeout)
                gap = after > self.cursor or (self.events and after < self.events[0][0]-1) or (not self.events and after < self.cursor)
                rows = []; total = 0; delivered = after
                for seq,event,size in self.events:
                    if seq <= after: continue
                    if self._scoped(self._event_thread(event)):
                        if rows and (total+size > MAX_FRAME//2 or len(rows) >= 200): break
                        rows.append({"seq":seq,"event":event}); total += size
                    delivered = seq
                if not self.events: delivered = self.cursor
                return {"events":rows,"cursor":delivered,"gap":bool(gap),"connected":self.connected,
                        "ready":self.ready,"ambiguous":not self.compatibility_check()}
        if command not in {"rpc","reply"}: raise SafeError("Unknown native broker operation.")
        deadline = time.monotonic() + self.rpc_timeout
        if not self.action_lock.acquire(timeout=self.rpc_timeout): raise SafeError("Native broker is busy. Nothing was sent.")
        try:
            if not self.ready: raise SafeError("Desktop engine is not ready. No operation was sent.")
            if time.monotonic() >= deadline: raise SafeError("Native operation deadline expired. Nothing was sent.")
            method, params = message.get("method"), message.get("params")
            if command == "rpc":
                self._validated_rpc(method,params)
                mutating = method in MUTATIONS
                if method not in {"thread/list","model/list"}: self._refresh_scope(params.get("threadId"),deadline)
            else:
                mutating = True; method = "reply"
                self._refresh_scope(message.get("thread_id"),deadline)
            operation = None
            with self.lock:
                if mutating:
                    operation, prior = self._journal_begin(message)
                    if prior is not None: return prior
                try:
                    if command == "reply":
                        row = self._reply_check(message)
                        self._claim(row,"telegram")
                        self.write_engine(wire({"id":message["request_id"],"result":message["result"]}))
                        result = {"response_sent":True,"native_confirmation_pending":True}
                    else:
                        self._active_check(method,params)
                        result = None
                except Exception:
                    if operation:
                        self.operations[operation]["state"] = "unknown"; self._persist()
                    raise
            try:
                if command == "rpc": result = self._request(method,params,deadline)
                if method == "thread/list":
                    if not isinstance(result.get("data"),list): raise SafeError("Unexpected native chat list.")
                    result = dict(result,data=[row for row in result["data"] if isinstance(row,dict) and self._allowed_path(row.get("cwd"))])
                with self.lock:
                    if operation:
                        self.operations[operation].update(state="completed",result=self._receipt(method,result)); self._persist()
                return result
            except Exception:
                if operation:
                    with self.lock: self.operations[operation]["state"] = "unknown"; self._persist()
                raise
        finally:
            self.action_lock.release()

    def close(self):
        with self.condition:
            self.connected = False
            if self.engine_spool is not None:
                self.engine_spool.close(); self.engine_spool = None; self.engine_routing = None
            for response in self.rpc_pending.values():
                try: response.put_nowait({"error":{"code":"disconnected"}})
                except queue.Full: pass
            self.condition.notify_all()


def is_engine_invocation(arguments):
    options = {"-c","--config","--enable","--disable","--listen","--code-mode-host","--profile","-p",
               "--ws-auth","--ws-token-file","--ws-token-sha256","--ws-shared-secret-file","--ws-issuer","--ws-audience","--ws-max-clock-skew-seconds"}
    positionals = []; skip = False
    for arg in arguments:
        if skip: skip=False; continue
        if arg in {"--help","-h","--version","-V"}: return False
        if arg in options: skip=True; continue
        if arg.startswith("-"): continue
        positionals.append(arg)
    return positionals == ["app-server"]


def _peer_is_owner(connection):
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    call = libc.getpeereid
    call.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    call.restype = ctypes.c_int
    return call(connection.fileno(),ctypes.byref(uid),ctypes.byref(gid)) == 0 and uid.value == os.getuid()


def _obsolete_ambiguity(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        identifier = value.get("pid")
        if value.get("schema") != "desktop-gateway-ambiguity/1" or type(identifier) is not int or identifier <= 0: return False
        try: os.kill(identifier,0)
        except ProcessLookupError: return True
        except PermissionError: return False
        return False
    except (OSError,ValueError,AttributeError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--real-cli", type=Path, required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if sys.platform != "darwin": raise SafeError("This Desktop gateway requires macOS.")
    from .macos_platform import verify_desktop, validate_parent
    home = args.home.expanduser().resolve()
    config = load(home)
    verified = verify_desktop(config)
    validate_parent(config)
    real = args.real_cli.resolve(strict=True)
    if real != Path(verified["real_cli"]).resolve(strict=True): raise SafeError("Native engine identity changed.")
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    environment = dict(os.environ)
    # Routing exception only: nested CLI calls must not recurse into this shim.
    environment["CODEX_CLI_PATH"] = str(real)
    if not is_engine_invocation(arguments): os.execve(str(real),[str(real),*arguments],environment)
    import fcntl
    lock_path = home / "native-gateway.lock"
    if lock_path.is_symlink(): raise SafeError("Native gateway lock is redirected.")
    lease = lock_path.open("a+b"); lock_path.chmod(0o600)
    ambiguity = home / "native-ambiguous.json"
    try:
        fcntl.flock(lease,fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Preserve the extra Desktop engine, but disable remote mutation until
        # the owner restarts Desktop with one verified local engine.
        atomic_json(ambiguity,{"schema":"desktop-gateway-ambiguity/1","pid":os.getpid(),"at":time.time()})
        lease.close()
        os.execve(str(real),[str(real),*arguments],environment)
    if ambiguity.exists() and _obsolete_ambiguity(ambiguity): ambiguity.unlink()
    operations_path = home / "native-operations.json"
    operations = {}
    if operations_path.exists():
        saved = json.loads(operations_path.read_text(encoding="utf-8"))
        if saved.get("schema") != "desktop-gateway-operations/1" or not isinstance(saved.get("operations"),dict): raise SafeError("Native operation journal is invalid.")
        operations = saved["operations"]
    directory = Path(tempfile.mkdtemp(prefix="ctd-" + str(os.getuid()) + "-",dir="/tmp"))
    directory.chmod(0o700)
    address = directory / "broker.sock"
    listener = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(str(address)); address.chmod(0o600); listener.listen(8); listener.settimeout(1)
    token = secrets.token_hex(32)
    child = None; core = None; stop = threading.Event(); slots = threading.BoundedSemaphore(8)
    manifest = home / "native-session.json"
    try:
        child = subprocess.Popen([str(real),*arguments],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None,env=environment,bufsize=0)
        def terminate(signum, frame):
            if child.poll() is None: child.send_signal(signum)
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM,terminate)
        signal.signal(signal.SIGINT,terminate)
        engine_lock, gui_lock = threading.Lock(), threading.Lock()
        def write(stream, lock, data):
            with lock: write_all(stream,data)
        core = GatewayCore(config,lambda data:write(child.stdin,engine_lock,data),lambda data:write(sys.stdout.buffer,gui_lock,data),
                           persist=lambda value:atomic_json(operations_path,value),operations=operations)
        core.compatibility_check = lambda: not ambiguity.exists()
        def save_manifest():
            atomic_json(manifest,{"schema":"desktop-gateway/1","epoch":core.epoch,"socket":str(address),"token":token,
                                  "pid":os.getpid(),"engine_pid":child.pid,"ready":core.ready,"ambiguous":not core.compatibility_check()})
        core.on_status = save_manifest
        save_manifest()
        def client(connection):
            try:
                connection.settimeout(35)
                if not _peer_is_owner(connection): return
                with connection.makefile("rb") as stream: body = stream.readline(MAX_FRAME+1)
                if len(body)>MAX_FRAME or not body.endswith(b"\n"): raise SafeError("Invalid native request frame.")
                request = json.loads(body)
                if not isinstance(request,dict) or not isinstance(request.get("token"),str) or not hmac.compare_digest(request["token"],token): raise SafeError("Native authentication failed.")
                result = core.command(request)
                response = wire({"ok":True,"result":result})
                if len(response)>MAX_FRAME: raise SafeError("Native response exceeds the safe limit.")
                connection.sendall(response)
            except Exception as exc:
                try: connection.sendall(wire({"ok":False,"error":str(exc) if isinstance(exc,SafeError) else "Native request failed."}))
                except OSError: pass
            finally:
                connection.close(); slots.release()
        def accept():
            while not stop.is_set():
                try: connection,_ = listener.accept()
                except socket.timeout: continue
                except OSError: break
                if not slots.acquire(blocking=False): connection.close(); continue
                threading.Thread(target=client,args=(connection,),daemon=True).start()
        threading.Thread(target=accept,daemon=True).start()
        def gui_input():
            try:
                while line := sys.stdin.buffer.readline(MAX_FRAME+1): core.from_gui(line)
            finally:
                try: child.stdin.close()
                except OSError: pass
        threading.Thread(target=gui_input,daemon=True).start()
        while line := child.stdout.readline(MAX_FRAME+1):
            core.from_engine(line)
        return child.wait()
    finally:
        stop.set(); listener.close()
        if core: core.close()
        if child and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
        # Never remove another engine generation's manifest or an arbitrary tree.
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            if core and value.get("epoch") == core.epoch: manifest.unlink()
        except (OSError,ValueError): pass
        try: address.unlink()
        except OSError: pass
        try: directory.rmdir()
        except OSError: pass
        lease.close()


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception:
        print("Desktop gateway could not start. Verify the local installation and compatibility.",file=sys.stderr)
        raise SystemExit(2)

"""Scope, lifecycle, and explicit decisions for this installation's chats only."""
from __future__ import annotations
import base64
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import re
import secrets
import threading
import time
from .config import SafeError, atomic_json
from .progress import read_progress

APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
UPLOAD_TYPES = {".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".xlsx", ".pptx",
                ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3", ".mp4", ".wav"}


class Controller:
    def __init__(self, config, home, rpc_factory):
        self.config, self.home = config, Path(home)
        self.workspace = Path(config["workspace"]).resolve()
        self.path = self.home / "state.json"
        self.lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.events = queue.Queue(maxsize=10000)
        self.notifications = queue.Queue(maxsize=1000)
        self.pending = {}
        self.tool_context = {}
        self.messages = {}
        self.loaded = set()
        self.active = {}
        self.statuses = {}
        self.last_error = None
        self.connected = False
        self.stop = threading.Event()
        self.data = {"threads": {}, "selected": None, "offset": 0, "requests": {}, "files": {}}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                for key in self.data:
                    if key in saved:
                        self.data[key] = saved[key]
            except (ValueError, OSError):
                raise SafeError("Local state is unreadable. Restore a backup; do not silently reset it.") from None
        for row in self.data["requests"].values():
            if row.get("status") == "dispatching":
                row["status"] = "unknown"
        self._save()
        self.rpc = rpc_factory(self._enqueue_event)
        self.connected = True
        self.worker = threading.Thread(target=self._event_loop, daemon=True)
        self.worker.start()

    def _save(self):
        atomic_json(self.path, self.data)

    def _enqueue_event(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            # Do not silently lose an approval or a terminal event.
            self.connected = False
            self.last_error = "Event backlog exceeded. Stop and reconcile before restarting."
            self.stop.set()

    def _notify(self, text):
        try:
            self.notifications.put_nowait(text[:16000])
        except queue.Full:
            self.last_error = "Notification backlog full; inspect the panel for results."

    def _event_loop(self):
        while not self.stop.is_set():
            try:
                event = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle_event(event)
            except Exception:
                self.last_error = "A Codex event could not be handled. Check compatibility."

    def require_thread(self, identifier):
        if identifier not in self.data["threads"]:
            raise SafeError("Chat is outside this installation's scope.")
        return identifier

    def handle_event(self, event):
        method, params = event.get("method"), event.get("params") or {}
        identifier = params.get("threadId")
        with self.lock:
            if method == "bridge/disconnected":
                self.connected = False
                self.last_error = "Codex disconnected. Pending operations need reconciliation."
                self.pending.clear()
                self.tool_context.clear()
                self.active.clear()
                return
            if "id" in event:
                if identifier not in self.data["threads"]:
                    self.rpc.reply(event["id"], error={"code": -32602, "message": "Out of scope"})
                    return
                if params.get("turnId") != self.active.get(identifier) or not self.active.get(identifier):
                    self.rpc.reply(event["id"], error={"code": -32602, "message": "Inactive turn"})
                    return
                if method not in APPROVALS | {"item/tool/requestUserInput"}:
                    self.rpc.reply(event["id"], error={"code": -32601, "message": "Unsupported request; use a compatible local client"})
                    self._notify("Codex requested an unsupported confirmation. It was not approved.")
                    return
                if method == "item/tool/requestUserInput" and any(q.get("isSecret") for q in params.get("questions", [])):
                    self.rpc.reply(event["id"], error={"code": -32601, "message": "Secret input is not supported over Telegram"})
                    self._notify("A secret input request was blocked. Use a local Codex session for this operation.")
                    return
                key = secrets.token_urlsafe(18)
                context = self.tool_context.get((identifier, params.get("turnId"), params.get("itemId")), {})
                review = {"command": params.get("command") or context.get("command"),
                          "changes": context.get("changes"), "network": params.get("networkApprovalContext"),
                          "permissions": params.get("additionalPermissions"), "cwd": params.get("cwd"),
                          "reason": params.get("reason"), "grantRoot": params.get("grantRoot")}
                reviewable = True
                if method == "item/commandExecution/requestApproval":
                    reviewable = bool(review["command"] or review["network"])
                elif method == "item/fileChange/requestApproval":
                    changes = review["changes"]
                    reviewable = isinstance(changes, list) and bool(changes) and all(
                        isinstance(c, dict) and isinstance(c.get("path"), str) and isinstance(c.get("diff"), str)
                        for c in changes)
                if len(json.dumps(review, ensure_ascii=False).encode()) > 200000:
                    review = {"notice": "Review context exceeds the safe display limit. Approve is disabled."}
                    reviewable = False
                self.pending[key] = {"id": event["id"], "key": key, "method": method,
                                     "params": params, "created_at": time.time(),
                                     "review_context": review, "reviewable": reviewable}
                self._notify("Codex is waiting for your decision. Open the panel to review the full request.")
                return
            if method == "serverRequest/resolved":
                request_id = params.get("requestId")
                self.pending = {k: v for k, v in self.pending.items()
                                if not (type(v["id"]) is type(request_id) and v["id"] == request_id
                                        and v["params"].get("threadId") == identifier)}
                return
            if identifier not in self.data["threads"]:
                return
            if method == "turn/started":
                self.active[identifier] = params.get("turn", {}).get("id")
                self.statuses[identifier] = "inProgress"
            elif method == "item/started":
                item = params.get("item", {})
                if item.get("type") in ("commandExecution", "fileChange"):
                    key = (identifier, params.get("turnId"), item.get("id"))
                    context = {k: item[k] for k in ("command", "changes") if k in item}
                    if len(json.dumps(context).encode()) <= 200000 and len(self.tool_context) < 500:
                        self.tool_context[key] = context
            elif method == "item/agentMessage/delta":
                items = self.messages.setdefault(identifier, [])
                item_id = params.get("itemId", "stream")
                if not items or items[-1].get("id") != item_id:
                    items.append({"id": item_id, "turn_id": params.get("turnId") or self.active.get(identifier), "role": "assistant", "text": ""})
                items[-1]["text"] = (items[-1]["text"] + str(params.get("delta", "")))[-100000:]
                del items[:-100]
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    items = self.messages.setdefault(identifier, [])
                    found = next((x for x in items if x.get("id") == item.get("id")), None)
                    if found:
                        found["text"] = str(item.get("text", found["text"]))[-100000:]
                    else:
                        items.append({"id": item.get("id"), "turn_id": params.get("turnId") or self.active.get(identifier), "role": "assistant", "text": str(item.get("text", ""))[-100000:]})
                    del items[:-100]
            elif method == "turn/completed":
                turn = params.get("turn", {})
                self.active.pop(identifier, None)
                self.statuses[identifier] = turn.get("status", "unknown")
                self.pending = {k: v for k, v in self.pending.items() if v["params"].get("threadId") != identifier}
                self.tool_context = {k: v for k, v in self.tool_context.items() if k[0] != identifier}
                items = self.messages.get(identifier, [])
                answer = next((x["text"] for x in reversed(items) if x.get("role") == "assistant" and x.get("turn_id") == turn.get("id")), "")
                self._notify("Codex · " + self.data["threads"][identifier]["title"] + ": " + self.statuses[identifier] + "\n" + answer)
            elif method == "error":
                self.statuses[identifier] = "error"
                self.last_error = "Codex reported an error. Review your local Codex diagnostics."

    def execute(self, request_id, action, payload):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise SafeError("A unique request ID is required.")
        digest = hashlib.sha256(json.dumps([action, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.action_lock:
            with self.lock:
                prior = self.data["requests"].get(request_id)
                if prior:
                    if prior["digest"] != digest:
                        raise SafeError("Request ID was already used for another operation.")
                    if prior.get("status") != "completed":
                        raise SafeError("The previous operation has an unknown outcome. Reconcile it; do not resend.")
                    return dict(prior)
                if len(self.data["requests"]) >= 20000:
                    raise SafeError("Operation ledger is full. Export and rotate it locally before continuing.")
                self.data["requests"][request_id] = {"digest": digest, "status": "dispatching", "action": action}
                self._save()
            try:
                result = self._action(action, payload)
                row = {"digest": digest, "status": "completed", "action": action, "result": result}
            except SafeError:
                with self.lock:
                    self.data["requests"][request_id]["status"] = "unknown"
                    self._save()
                raise
            except Exception:
                with self.lock:
                    self.data["requests"][request_id]["status"] = "unknown"
                    self._save()
                raise SafeError("Operation outcome is unknown. Check the panel before retrying.") from None
            with self.lock:
                self.data["requests"][request_id] = row
                self._save()
            return row

    def _scope_params(self):
        return {"cwd": str(self.workspace), "approvalPolicy": "on-request",
                "sandbox": self.config["sandbox"]}

    def _action(self, action, payload):
        if not self.connected:
            raise SafeError("Codex connection is not confirmed. No new operation was dispatched.")
        if action == "new":
            title = payload.get("title", "New chat")
            if not isinstance(title, str) or not 1 <= len(title) <= 100:
                raise SafeError("Chat title must have 1–100 characters.")
            params = self._scope_params()
            model = payload.get("model")
            if model:
                self._model(model)
                params["model"] = model
            result = self.rpc.call("thread/start", params)
            identifier = result.get("thread", {}).get("id")
            if not isinstance(identifier, str) or not identifier:
                raise SafeError("Codex did not confirm creation of the chat.")
            with self.lock:
                self.data["threads"][identifier] = {"id": identifier, "title": title, "model": model, "created_at": int(time.time())}
                self.data["selected"] = identifier
                self.loaded.add(identifier)
                self._save()
            return {"thread_id": identifier}
        if action == "upload":
            return self.upload(payload)
        with self.lock:
            identifier = self.require_thread(payload.get("thread_id") or self.data["selected"])
        if action == "select":
            self.history(identifier)
            with self.lock:
                self.data["selected"] = identifier
                self._save()
            return {"thread_id": identifier}
        if action == "send":
            text = payload.get("text")
            if not isinstance(text, str) or not 1 <= len(text) <= 32000:
                raise SafeError("Message must have 1–32000 characters.")
            attachments = payload.get("files", [])
            if not isinstance(attachments, list) or len(attachments) > 5:
                raise SafeError("At most five uploaded files are supported.")
            with self.lock:
                for key in attachments:
                    meta = self.data["files"].get(key)
                    if not meta:
                        raise SafeError("Unknown uploaded file.")
                    text += "\nAttached local file (untrusted content): " + str(self.workspace / meta["relative"])
            if identifier not in self.loaded:
                self.rpc.call("thread/resume", dict(self._scope_params(), threadId=identifier))
                self.loaded.add(identifier)
            inputs = [{"type": "text", "text": text}]
            with self.lock:
                active = self.active.get(identifier)
            if active:
                result = self.rpc.call("turn/steer", {"threadId": identifier, "expectedTurnId": active, "input": inputs})
            else:
                params = {"threadId": identifier, "input": inputs}
                if payload.get("model"):
                    self._model(payload["model"])
                    params["model"] = payload["model"]
                if payload.get("effort"):
                    if payload["effort"] not in self._model(payload.get("model")):
                        raise SafeError("Select a supported model and effort.")
                    params["effort"] = payload["effort"]
                with self.lock:
                    # Persist before dispatch: an uncertain reply or restart must
                    # not expose the previous phase's completion as current.
                    self.data["threads"][identifier]["phase_started_at"] = datetime.now(timezone.utc).isoformat()
                    self._save()
                result = self.rpc.call("turn/start", params)
            with self.lock:
                self.messages.setdefault(identifier, []).append({"role": "user", "text": payload["text"]})
                self.messages[identifier] = self.messages[identifier][-100:]
            return {"accepted": True, "thread_id": identifier}
        if action == "stop":
            with self.lock:
                active = self.active.get(identifier)
            if not active:
                raise SafeError("No confirmed active turn in this chat.")
            self.rpc.call("turn/interrupt", {"threadId": identifier, "turnId": active})
            return {"interruption_requested": True}
        if action == "decision":
            return self.answer(identifier, payload)
        raise SafeError("Unknown action.")

    def models(self):
        result = self.rpc.call("model/list", {})
        return [{"id": x.get("id"), "model": x.get("model", x.get("id")),
                 "name": x.get("displayName", x.get("id")),
                 "efforts": [e.get("reasoningEffort") for e in x.get("supportedReasoningEfforts", [])]}
                for x in result.get("data", []) if x.get("id")]

    def _model(self, model):
        for row in self.models():
            if model == row["model"]:
                return row["efforts"]
        raise SafeError("Model is not in the current Codex catalog. Choose the default or refresh.")

    def answer(self, identifier, payload):
        with self.lock:
            row = self.pending.get(payload.get("key"))
            if not row or row["params"].get("threadId") != identifier:
                raise SafeError("This request has expired or was already resolved.")
            if row.get("submitted"):
                raise SafeError("A response was already sent; wait for native confirmation.")
            if time.time() - row["created_at"] > 600:
                raise SafeError("Decision expired. Stop this turn and request a new one.")
            if row["method"] in APPROVALS:
                choice = payload.get("decision")
                if choice not in ("accept", "decline"):
                    raise SafeError("Only a single explicit approval or decline is supported.")
                if choice == "accept" and not row.get("reviewable"):
                    raise SafeError("Full operation details are unavailable. Approval is disabled; decline or stop this turn.")
                allowed = row["params"].get("availableDecisions")
                if isinstance(allowed, list) and choice not in allowed:
                    raise SafeError("This decision is not offered by Codex.")
                result = {"decision": choice}
            else:
                given = payload.get("answers")
                questions = row["params"].get("questions", [])
                if not isinstance(given, dict) or set(given) != {q.get("id") for q in questions}:
                    raise SafeError("Answer every question in this request.")
                result = {"answers": {}}
                for question in questions:
                    answer = given[question["id"]]
                    if not isinstance(answer, str) or not 1 <= len(answer) <= 8000:
                        raise SafeError("Answer must have 1–8000 characters.")
                    result["answers"][question["id"]] = {"answers": [answer]}
            # Reserve before the write: even a partial write must never be replayed.
            row["submitted"] = True
            self.rpc.reply(row["id"], result)
            return {"response_sent": True, "native_confirmation_pending": True}

    def history(self, identifier):
        with self.lock:
            self.require_thread(identifier)
            if identifier in self.active:
                return
        result = self.rpc.call("thread/read", {"threadId": identifier, "includeTurns": True})
        items = []
        for turn in result.get("thread", {}).get("turns", []):
            for item in turn.get("items", []):
                if item.get("type") == "agentMessage":
                    items.append({"id": item.get("id"), "turn_id": turn.get("id"), "role": "assistant", "text": str(item.get("text", ""))[-100000:]})
                elif item.get("type") == "userMessage":
                    text = "\n".join(x.get("text", "") for x in item.get("content", []) if x.get("type") == "text")
                    items.append({"id": item.get("id"), "role": "user", "text": text[-100000:]})
        with self.lock:
            self.messages[identifier] = items[-100:]

    def upload(self, payload):
        name = payload.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[^\\/:\x00-\x1f]{1,120}", name):
            raise SafeError("Invalid file name.")
        suffix = Path(name).suffix.lower()
        if suffix not in UPLOAD_TYPES:
            raise SafeError("Unsupported attachment type.")
        encoded = payload.get("content", "")
        if not isinstance(encoded, str) or len(encoded) > 14_000_000:
            raise SafeError("Attachment exceeds 10 MB.")
        try:
            body = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise SafeError("Invalid attachment encoding.") from None
        if len(body) > 10_000_000:
            raise SafeError("Attachment exceeds 10 MB.")
        directory = self.workspace / ".telegram-attachments"
        if directory.is_symlink() or (directory.exists() and directory.resolve().parent != self.workspace):
            raise SafeError("Attachment directory is redirected.")
        directory.mkdir(mode=0o700, exist_ok=True)
        key = secrets.token_urlsafe(18)
        path = directory / (key + suffix)
        with path.open("xb") as stream:
            stream.write(body)
        path.chmod(0o600)
        with self.lock:
            self.data["files"][key] = {"name": name, "relative": str(path.relative_to(self.workspace)), "size": len(body)}
            self._save()
        return {"file_id": key, "name": name}

    def files(self):
        # Export requires placing a file in a dedicated directory; no arbitrary path API.
        root = self.workspace / "telegram-exports"
        if not root.exists():
            return []
        if root.is_symlink() or root.resolve().parent != self.workspace:
            raise SafeError("Export directory is redirected.")
        result = []
        for path in sorted(root.iterdir()):
            if (path.is_file() and not path.is_symlink() and path.resolve().parent == root.resolve()
                    and path.suffix.lower() in UPLOAD_TYPES and path.stat().st_size <= 10_000_000):
                result.append({"name": path.name, "size": path.stat().st_size})
            if len(result) >= 200:
                break
        return result

    def download(self, name):
        if name not in {x["name"] for x in self.files()}:
            raise SafeError("File is outside the explicit export directory or exceeds 10 MB.")
        path = self.workspace / "telegram-exports" / name
        # Recheck immediately before opening; a hostile same-user process is outside this boundary.
        if path.is_symlink() or path.resolve().parent != (self.workspace / "telegram-exports").resolve():
            raise SafeError("Redirected export is not allowed.")
        with path.open("rb") as stream:
            body = stream.read(10_000_001)
        if len(body) > 10_000_000:
            raise SafeError("File exceeds 10 MB.")
        return body

    def snapshot(self):
        with self.lock:
            selected = self.data["selected"]
            return {"connected": self.connected, "error": self.last_error,
                    "threads": [dict(row, status=self.statuses.get(key, "notLoaded")) for key, row in self.data["threads"].items()],
                    "selected": selected, "messages": list(self.messages.get(selected, [])),
                    "pending": [{"key": key, "method": row["method"], "params": row["params"], "submitted": row.get("submitted", False),
                                 "review_context": row.get("review_context"), "reviewable": row.get("reviewable", False)}
                                for key, row in self.pending.items() if row["params"].get("threadId") == selected],
                    "uncertain_operations": sum(row.get("status") in ("unknown", "dispatching") for row in self.data["requests"].values()),
                    "mode": "isolated-app-server", "sandbox": self.config["sandbox"],
                    "progress": read_progress(self.workspace, selected,
                        self.data["threads"][selected].get("phase_started_at")) if selected else None}

    def close(self):
        self.stop.set()
        self.rpc.close()
        if hasattr(self, "worker"):
            self.worker.join(timeout=2)

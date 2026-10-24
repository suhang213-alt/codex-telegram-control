"""Docker front end for a trusted local Desktop host connector; no engine."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from .config import SafeError
from .security import authenticate
from .relay_protocol import (MAX_BODY, MAX_WIRE, MAX_PENDING_BYTES, PUBLIC_ASSETS, TELEGRAM_METHODS,
                             READ_METHODS, Verifier, binding, filter_headers, json_bytes, pack_body,
                             public_origin, read_secret, signed, unpack_body, validate_route)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SafeError("Relay redirects are disabled.")


def telegram_http(token, method, payload, *, document=None, file_path=None):
    """Only the fixed Telegram service; never use caller-supplied URLs."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    if file_path is not None:
        if not isinstance(file_path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,300}", file_path) or ".." in file_path.split("/") or file_path.startswith("/"):
            raise SafeError("Invalid Telegram file path.")
        request = urllib.request.Request("https://api.telegram.org/file/bot" + token + "/" + file_path)
    elif document is not None:
        name, content = document["name"], unpack_body(document["body"])
        if not isinstance(name, str) or not re.fullmatch(r'[^\r\n"\\/]{1,120}', name):
            raise SafeError("Invalid document name.")
        boundary = "ctd" + secrets.token_hex(16)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{payload["chat_id"]}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + content + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendDocument", data=body,
                                         headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    else:
        request = urllib.request.Request("https://api.telegram.org/bot" + token + "/" + method,
                                         data=json_bytes(payload), headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY: raise SafeError("Telegram response exceeds the relay limit.")
        if file_path is not None: return {"body": pack_body(raw)}
        result = json.loads(raw)
        if result.get("ok") is not True: raise ValueError()
        return result.get("result")
    except (OSError, ValueError, urllib.error.URLError):
        raise SafeError("Telegram request failed; delivery may be uncertain. No automatic resend.") from None


class Relay:
    LEASE_SECONDS = 45

    def __init__(self, key, token, owner, public_url, state_dir, *, clock=time.monotonic, telegram=telegram_http):
        self.key, self.token, self.owner = key, token, owner
        self.public_url = public_origin(public_url)
        self.clock, self.telegram = clock, telegram
        self.epoch = secrets.token_hex(16)
        self.verifier = Verifier(key)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.host = None
        self.jobs = {}
        self.pending_bytes = 0
        self.input_bytes = 0
        self.tickets = {}
        self.recent = []
        folder = Path(state_dir)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.is_symlink() or (folder / "relay.sqlite3").is_symlink(): raise SafeError("Unsafe relay state directory.")
        self.database = folder / "relay.sqlite3"
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL)")
            db.execute("UPDATE operations SET state='unknown' WHERE state='dispatching'")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=5)
        try:
            with db: yield db
        finally: db.close()

    def connected(self):
        with self.lock:
            return bool(self.host and self.host["ready"] and self.clock() - self.host["seen"] < self.LEASE_SECONDS)

    def _host(self, value):
        if (not self.host or value.get("epoch") != self.epoch or value.get("lease") != self.host["lease"]
                or self.clock() - self.host["seen"] >= self.LEASE_SECONDS):
            raise SafeError("Host lease expired or changed. Pending outcomes must be reconciled.")
        self.host["seen"] = self.clock()

    def control(self, action, value):
        if not isinstance(value, dict): raise SafeError("Invalid relay operation.")
        if action == "hello": return {"epoch": self.epoch}
        if action == "join":
            if (value.get("epoch") != self.epoch or value.get("owner_id") != self.owner or value.get("public_url") != self.public_url
                    or not isinstance(value.get("bot_binding"), str)
                    or not hmac.compare_digest(value["bot_binding"], binding(self.token))
                    or value.get("platform") not in {"macos", "windows"}
                    or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("host_id", "")))):
                raise SafeError("Host configuration does not match this relay.")
            with self.condition:
                if self.host and self.clock() - self.host["seen"] < self.LEASE_SECONDS:
                    if self.host["id"] != value["host_id"]: raise SafeError("Another host connector holds the active lease.")
                else:
                    for row in self.jobs.values():
                        row["state"] = "unknown"
                    self.host = {"id": value["host_id"], "lease": secrets.token_hex(24), "ready": False}
                    self.tickets.clear()
                    self.condition.notify_all()
                self.host["seen"] = self.clock()
                return {"epoch": self.epoch, "lease": self.host["lease"]}
        with self.condition:
            self._host(value)
            if action == "poll":
                self.host["ready"] = value.get("backend_ready") is True
                if not self.host["ready"]: return {"job": None}
                deadline = self.clock() + 8
                while True:
                    self._host(value)
                    for identifier, row in self.jobs.items():
                        if row["state"] == "queued":
                            row["state"] = "claimed"
                            row["lease"] = value["lease"]
                            return {"job": {"id": identifier, **row["request"]}}
                    remaining = deadline - self.clock()
                    if remaining <= 0: return {"job": None}
                    self.condition.wait(min(remaining, 1))
            if action == "complete":
                row = self.jobs.get(value.get("id"))
                if not row or row["state"] != "claimed" or row.get("lease") != value["lease"]:
                    raise SafeError("Relay completion is late, duplicated, or belongs to another lease.")
                result = value.get("response")
                if not isinstance(result, dict) or type(result.get("status")) is not int or not 200 <= result["status"] <= 599:
                    raise SafeError("Invalid host response.")
                body = unpack_body(result.get("body"))
                headers = filter_headers(result.get("headers", {}), response=True)
                length = result.get("entity_length", len(body))
                if type(length) is not int or not 0 <= length <= MAX_BODY or (row["request"]["method"] != "HEAD" and length != len(body)):
                    raise SafeError("Invalid host response length.")
                row["response"] = {"status": result["status"], "headers": headers, "body": body, "entity_length": length}
                row["state"] = "completed"
                self._cache_ticket(row)
                self.condition.notify_all()
                return {"accepted": True}
        if action == "telegram": return self._telegram(value)
        raise SafeError("Relay operation is unavailable.")

    def _cache_ticket(self, row):
        if (row["request"]["method"] != "POST" or not re.fullmatch(r"/api/threads/[A-Za-z0-9-]{1,100}/artifacts/[0-9a-f]{32}/download-ticket", row["request"]["path"])
                or row["response"]["status"] != 200): return
        try:
            value = json.loads(row["response"]["body"])
            target, expires = value["url"], value["expiresAt"]
            if (not isinstance(target, str) or not re.fullmatch(r"/api/artifact-download/[A-Za-z0-9_-]{40,64}", target)
                    or type(expires) not in (int, float) or not 0 < expires - time.time() <= 3600): return
            self.tickets = {k: v for k, v in self.tickets.items() if v > time.time()}
            if len(self.tickets) >= 1000: return
            self.tickets[target] = min(expires, time.time() + 600)
        except (ValueError, KeyError, TypeError): pass

    def authorize_public(self, method, target, headers):
        path = validate_route(method, target)
        if method in {"GET", "HEAD"} and path in PUBLIC_ASSETS: return
        with self.lock:
            if method in {"GET", "HEAD", "OPTIONS"} and self.tickets.get(path, 0) > time.time(): return
        try: authenticate(headers.get("x-telegram-init-data", ""), self.token, self.owner)
        except (ValueError, TypeError): raise SafeError("Invalid Telegram owner authorization.") from None

    def forward(self, method, target, headers, body, *, timeout=30):
        validate_route(method, target)
        headers = filter_headers(headers)
        self.authorize_public(method, target, headers)
        request = {"method": method, "path": target, "headers": headers, "body": pack_body(body)}
        size = len(json_bytes(request))
        with self.condition:
            if not self.connected(): raise SafeError("Desktop host connector is not connected.")
            self.recent = [stamp for stamp in self.recent if self.clock() - stamp < 10]
            if len(self.recent) >= 50 or len(self.jobs) >= 16 or self.pending_bytes + size > MAX_PENDING_BYTES:
                raise SafeError("Relay is busy. No new operation was queued.")
            self.recent.append(self.clock())
            identifier = secrets.token_hex(16)
            row = {"state": "queued", "request": request}
            self.jobs[identifier] = row
            self.pending_bytes += size
            self.condition.notify_all()
            deadline = self.clock() + timeout
            try:
                while row["state"] not in {"completed", "unknown"}:
                    remaining = deadline - self.clock()
                    if remaining <= 0: break
                    self.condition.wait(min(remaining, 1))
                if row["state"] != "completed":
                    raise SafeError("Host response was not confirmed; the outcome may be uncertain. Check Desktop before retrying.")
                return row["response"]
            finally:
                self.jobs.pop(identifier, None)
                self.pending_bytes -= size

    def _telegram(self, value):
        method, payload = value.get("method"), value.get("payload")
        file_path, document = value.get("file_path"), value.get("document")
        if method not in TELEGRAM_METHODS or not isinstance(payload, dict): raise SafeError("Telegram operation is unavailable.")
        if method in {"sendMessage", "sendDocument", "setChatMenuButton"} and payload.get("chat_id") != self.owner:
            raise SafeError("Telegram recipient must be the configured owner.")
        if method == "setMyCommands":
            scope = payload.get("scope", {})
            if scope != {"type": "chat", "chat_id": self.owner}: raise SafeError("Telegram command scope must be the owner chat.")
        if file_path is not None and method != "getFile": raise SafeError("Invalid Telegram file operation.")
        if document is not None and (method != "sendDocument" or not isinstance(document, dict)):
            raise SafeError("Invalid Telegram document operation.")
        if method == "getUpdates":
            payload = dict(payload, timeout=min(20, max(0, int(payload.get("timeout", 20)))))
        mutating = method not in READ_METHODS
        operation = value.get("operation_id")
        if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation): raise SafeError("Invalid relay operation ID.")
        if mutating:
            digest = hashlib.sha256(json_bytes({k: value.get(k) for k in ("method", "payload", "document")})).hexdigest()
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM operations WHERE id=?", (operation,)).fetchone():
                    raise SafeError("Telegram operation was already dispatched. No automatic resend.")
                db.execute("INSERT INTO operations VALUES(?,?,'dispatching')", (operation, digest))
        try:
            result = self.telegram(self.token, method, payload, document=document, file_path=file_path)
        except Exception:
            if mutating:
                with self._db() as db: db.execute("UPDATE operations SET state='unknown' WHERE id=?", (operation,))
            raise SafeError("Telegram delivery is uncertain. Check Telegram before retrying.") from None
        if mutating:
            with self._db() as db: db.execute("UPDATE operations SET state='completed' WHERE id=?", (operation,))
        if method == "getUpdates" and isinstance(result, list):
            filtered = []
            for update in result:
                if not isinstance(update, dict) or type(update.get("update_id")) is not int: continue
                callback = update.get("callback_query") or {}
                message = update.get("message") or callback.get("message") or {}
                user = callback.get("from") or message.get("from") or {}
                chat = message.get("chat") or {}
                if user.get("id") == self.owner and chat.get("id") == self.owner and chat.get("type") == "private":
                    filtered.append(update)
                else:
                    # Preserve offset advancement without forwarding someone
                    # else's content. Host owner checks reject this sentinel.
                    filtered.append({"update_id": update["update_id"]})
            result = filtered
        return {"result": result}


def handler(relay, *, control=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def setup(self):
            super().setup(); self.connection.settimeout(40)
        def reply(self, status, body, headers=None, entity_length=None):
            self.send_response(status)
            for key, value in (headers or {"content-type": "application/json"}).items(): self.send_header(key, value)
            self.send_header("Content-Length", str(entity_length if self.command == "HEAD" and entity_length is not None else len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD": self.wfile.write(body)
        def body(self, maximum):
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                raise SafeError("Ambiguous relay body framing.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= maximum: raise SafeError("Relay body exceeds the limit.")
            if self.command in {"GET", "HEAD", "OPTIONS"} and length:
                raise SafeError("Read-only relay requests cannot contain a body.")
            with relay.lock:
                if relay.input_bytes + length > 32_000_000: raise SafeError("Relay input budget is busy.")
                relay.input_bytes += length
                self._reserved = length
            value = self.rfile.read(length)
            if len(value) != length: raise SafeError("Incomplete relay body.")
            return value
        def dispatch(self):
            self._reserved = 0
            try:
                if control:
                    if self.command != "POST" or self.path not in {"/hello", "/join", "/poll", "/complete", "/telegram"}:
                        return self.reply(404, b'{}')
                    envelope = json.loads(self.body(MAX_WIRE if self.path in {"/complete", "/telegram"} else 16_384))
                    value = relay.verifier.verify("request:" + self.path, envelope)
                    try: result = {"ok": True, "result": relay.control(self.path[1:], value)}
                    except SafeError as exc: result = {"ok": False, "error": str(exc)}
                    answer = signed(relay.key, "response:" + self.path, result, nonce=envelope["nonce"])
                    return self.reply(200, json_bytes(answer))
                if self.headers.get("Host") not in {urllib.parse.urlsplit(relay.public_url).netloc, "127.0.0.1:8789", "localhost:8789", "localhost:8787", "127.0.0.1:8787"}:
                    return self.reply(403, b'{}')
                if self.path == "/healthz" and self.command == "GET":
                    return self.reply(200, json_bytes({"ok": True, "service": "codex-telegram-relay", "host_connected": relay.connected()}))
                allowed_origins = {None, relay.public_url}
                if re.fullmatch(r"/api/artifact-download/[A-Za-z0-9_-]{40,64}", self.path):
                    allowed_origins.add("https://web.telegram.org")
                if self.headers.get("Origin") not in allowed_origins: return self.reply(403, b'{}')
                headers = filter_headers(dict(self.headers.items()))
                # Authenticate before reading a potentially large body or queuing.
                relay.authorize_public(self.command, self.path, headers)
                result = relay.forward(self.command, self.path, headers, self.body(MAX_BODY))
                return self.reply(result["status"], result["body"], result["headers"], result.get("entity_length"))
            except SafeError as exc:
                return self.reply(503, json_bytes({"error": str(exc)}))
            except (OSError, ValueError, TypeError, KeyError):
                return self.reply(400, b'{"error":"Invalid relay request"}')
            except Exception:
                return self.reply(503, b'{"error":"Relay operation unavailable; no automatic resend"}')
            finally:
                if self._reserved:
                    with relay.lock: relay.input_bytes -= self._reserved
        do_GET = do_HEAD = do_POST = do_OPTIONS = dispatch
    return Handler


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    def __init__(self, address, handler_type):
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(address, handler_type)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False): self.shutdown_request(request); return
        try: super().process_request(request, address)
        except Exception: self.slots.release(); raise
    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.slots.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Docker relay for existing Desktop host adapters; never starts a model.")
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--state-dir", default="/data")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not 0 < args.owner_id < 2**52: raise SafeError("Invalid configured owner ID.")
        relay = Relay(read_secret(args.key_file), read_secret(args.token_file, token=True), args.owner_id, args.public_url, args.state_dir)
        if args.self_check:
            print(json.dumps({"ok": True, "configuration_valid": True, "host_connected": False, "network": "not_run"})); return 0
        public = BoundedServer(("0.0.0.0", 8787), handler(relay))
        private = BoundedServer(("0.0.0.0", 8788), handler(relay, control=True))
        threading.Thread(target=private.serve_forever, daemon=True).start()
        try: public.serve_forever()
        except KeyboardInterrupt: pass
        finally: private.shutdown(); private.server_close(); public.server_close()
        return 0
    except (SafeError, OSError, ValueError) as exc:
        print(str(exc) if isinstance(exc, SafeError) else "Relay could not start; check configuration and local ports.")
        return 2


if __name__ == "__main__": raise SystemExit(main())

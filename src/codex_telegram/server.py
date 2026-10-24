"""Loopback HTTP API. Only Telegram-signed owner requests can read or mutate state."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from .config import SafeError
from .security import authenticate

STATIC = Path(__file__).with_name("static")
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}


def handler_factory(controller, telegram, token, config):
    public = config["public_url"]
    hosts = {urlsplit(public).netloc, "127.0.0.1:" + str(config["port"]), "localhost:" + str(config["port"])}
    rate_lock = threading.Lock()
    recent = []
    class Handler(BaseHTTPRequestHandler):
        server_version = "PrivatePanel"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_args):
            pass  # No URLs, headers, tokens, user messages or files in access logs.

        def respond(self, status, value, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org")
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def valid_host(self):
            return self.headers.get("Host") in hosts

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {"error": "Invalid host"})
            if self.path == "/healthz":
                return self.respond(200, {"ok": True, "service": "codex-telegram-control"})
            asset = ASSETS.get(self.path)
            if asset:
                return self.respond(200, (STATIC / asset[0]).read_bytes(), asset[1] + "; charset=utf-8")
            return self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if not self.valid_host() or self.headers.get("Origin") not in (None, public):
                return self.respond(403, {"error": "Invalid origin"})
            try:
                authenticate(self.headers.get("X-Telegram-Init-Data", ""), token, config["owner_id"])
            except (SafeError, ValueError, TypeError):
                return self.respond(401, {"error": "Reopen the panel from your Telegram bot."})
            with rate_lock:
                now = time.monotonic()
                recent[:] = [stamp for stamp in recent if now - stamp < 10]
                if len(recent) >= 50:
                    return self.respond(429, {"error": "Too many requests. Wait a few seconds."})
                recent.append(now)
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                return self.respond(415, {"error": "JSON is required"})
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                maximum = 14_100_000 if self.path == "/api/action" else 65536
                if not 2 <= length <= maximum or self.headers.get("Transfer-Encoding"):
                    return self.respond(413, {"error": "Invalid body size"})
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.respond(400, {"error": "Incomplete request"})
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError()
                if self.path == "/api/state":
                    return self.respond(200, dict(controller.snapshot(), telegram_status=telegram.status,
                                                 telegram_error=telegram.last_error))
                if self.path == "/api/models":
                    return self.respond(200, {"models": controller.models()})
                if self.path == "/api/files":
                    return self.respond(200, {"files": controller.files()})
                if self.path == "/api/action":
                    payload = data.get("payload", {})
                    if not isinstance(payload, dict):
                        raise ValueError()
                    return self.respond(200, controller.execute(data.get("request_id"), data.get("action"), payload))
                if self.path == "/api/download":
                    return self.respond(200, controller.download(data.get("name")), "application/octet-stream")
                return self.respond(404, {"error": "Not found"})
            except SafeError as exc:
                return self.respond(409, {"error": str(exc)})
            except (ValueError, TypeError, OSError):
                return self.respond(400, {"error": "Invalid request or local resource unavailable"})
            except Exception:
                return self.respond(503, {"error": "Operation failed; check status before retrying"})
    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

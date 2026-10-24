"""Bounded, authenticated local Docker transport; never a general HTTP proxy."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, parse_qsl
from .config import SafeError

CONTROL_URL = "http://127.0.0.1:8788"
HOST_URL = "http://127.0.0.1:8787"
MAX_BODY = 21_000_000
MAX_WIRE = 30_000_000
MAX_PENDING_BYTES = 64_000_000
FORWARD_HEADERS = {"content-type", "origin", "x-telegram-init-data", "if-none-match", "range"}
RESPONSE_HEADERS = {"content-type", "content-disposition", "content-encoding", "etag", "vary",
                    "content-security-policy", "x-content-type-options", "referrer-policy",
                    "access-control-allow-origin", "access-control-allow-methods",
                    "access-control-allow-headers", "content-range", "accept-ranges"}
PUBLIC_ASSETS = {"/", "/index.html", "/app.js", "/style.css", "/styles.css", "/favicon.ico",
                 "/marble.svg", "/task-progress.js", "/task-progress.css"}
TELEGRAM_METHODS = {"getMe", "getWebhookInfo", "getUpdates", "sendMessage", "sendDocument",
                    "setMyCommands", "setChatMenuButton", "answerCallbackQuery", "getFile"}
READ_METHODS = {"getMe", "getWebhookInfo", "getUpdates", "getFile"}
READ_ROUTES = (
    r"/api/(?:threads|projects|archived|usage|decisions)",
    r"/api/threads/[A-Za-z0-9-]{1,100}(?:/progress|/artifacts)?",
    r"/api/files/[a-f0-9]{32}", r"/api/operations/[0-9]{1,20}",
    r"/api/artifact-download/[A-Za-z0-9_-]{40,64}",
)
WRITE_ROUTES = (
    r"/api/(?:state|models|files|action|download|uploads|tasks)",
    r"/api/(?:usage/refresh|files/import|voice/transcribe)",
    r"/api/decisions/[a-f0-9]{20}/answer",
    r"/api/threads/[A-Za-z0-9-]{1,100}/(?:history|actions|messages)",
    r"/api/threads/[A-Za-z0-9-]{1,100}/artifacts/[0-9a-f]{32}/download-ticket",
)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_secret(path, *, token=False):
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024:
            raise ValueError()
        value = path.read_text(encoding="utf-8").strip()
        pattern = r"[0-9]{5,16}:[A-Za-z0-9_-]{20,100}" if token else r"[0-9a-f]{64}"
        if not re.fullmatch(pattern, value): raise ValueError()
        return value if token else bytes.fromhex(value)
    except (OSError, ValueError):
        raise SafeError("Relay secret file is unavailable or invalid.") from None


def public_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise SafeError("Relay public URL must be an HTTPS origin.")
    return value.rstrip("/")


def binding(token):
    return hmac.new(token.encode(), b"codex-telegram-relay-bot-binding/1", hashlib.sha256).hexdigest()


def pack_body(body):
    if not isinstance(body, bytes) or len(body) > MAX_BODY: raise SafeError("Relay body exceeds the limit.")
    return base64.b64encode(body).decode("ascii")


def unpack_body(value):
    try:
        if not isinstance(value, str) or len(value) > (MAX_BODY + 2) // 3 * 4: raise ValueError()
        result = base64.b64decode(value, validate=True)
        if len(result) > MAX_BODY: raise ValueError()
        return result
    except (ValueError, TypeError):
        raise SafeError("Invalid relay body.") from None


def validate_route(method, target):
    if method not in {"GET", "HEAD", "POST", "OPTIONS"} or not isinstance(target, str) or len(target) > 4096:
        raise SafeError("Relay route is unavailable.")
    parsed = urlsplit(target)
    if (not target.startswith("/") or target.startswith("//") or parsed.scheme or parsed.netloc
            or parsed.fragment or "\\" in target or any(ord(c) < 32 for c in target)
            or "%" in parsed.path or ".." in parsed.path.split("/")):
        raise SafeError("Invalid relay route.")
    path = parsed.path
    patterns = WRITE_ROUTES if method == "POST" else READ_ROUTES
    allowed = any(re.fullmatch(pattern, path) for pattern in patterns)
    if method in {"GET", "HEAD"} and path in PUBLIC_ASSETS | {"/healthz"}: allowed = True
    if method == "OPTIONS": allowed = bool(re.fullmatch(r"/api/artifact-download/[A-Za-z0-9_-]{40,64}", path))
    if not allowed: raise SafeError("Relay route is unavailable.")
    try: query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError: raise SafeError("Invalid relay query parameters.") from None
    keys = [key for key, _ in query]
    permitted = {"thread", "decision"} if path in {"/", "/index.html"} else {"thread"} if path == "/api/decisions" else {"before"} if re.fullmatch(r"/api/threads/[A-Za-z0-9-]{1,100}", path) else set()
    if len(keys) != len(set(keys)) or set(keys) - permitted or any(len(value) > 1000 for _, value in query):
        raise SafeError("Invalid relay query parameters.")
    return path


def filter_headers(values, *, response=False):
    allowed = RESPONSE_HEADERS if response else FORWARD_HEADERS
    result = {}
    if not isinstance(values, dict) or len(values) > 32: raise SafeError("Invalid relay headers.")
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(value) > 16384 or "\r" in value or "\n" in value:
            raise SafeError("Invalid relay headers.")
        if key.lower() == "range" and not re.fullmatch(r"bytes=(?:[0-9]{1,12}-[0-9]{0,12}|-[0-9]{1,12})", value):
            raise SafeError("Invalid download range.")
        if key.lower() in allowed: result[key.lower()] = value
    return result


def signed(key, channel, payload, *, nonce=None, now=None):
    envelope = {"v": 1, "at": int(time.time() if now is None else now),
                "nonce": nonce or secrets.token_hex(24), "payload": payload}
    envelope["mac"] = hmac.new(key, channel.encode() + b"\n" + json_bytes(envelope), hashlib.sha256).hexdigest()
    return envelope


class Verifier:
    def __init__(self, key, *, clock=time.time):
        self.key, self.clock, self.seen, self.lock = key, clock, {}, threading.Lock()

    def verify(self, channel, envelope, *, expected_nonce=None):
        if not isinstance(envelope, dict) or set(envelope) != {"v", "at", "nonce", "payload", "mac"}:
            raise SafeError("Invalid relay authentication.")
        stamp, nonce = envelope["at"], envelope["nonce"]
        if (type(envelope["v"]) is not int or envelope["v"] != 1 or type(stamp) is not int
                or not -5 <= self.clock() - stamp <= 60 or not isinstance(nonce, str)
                or not re.fullmatch(r"[0-9a-f]{48}", nonce) or not isinstance(envelope["mac"], str)):
            raise SafeError("Relay authentication expired or invalid.")
        content = {k: v for k, v in envelope.items() if k != "mac"}
        expected = hmac.new(self.key, channel.encode() + b"\n" + json_bytes(content), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, envelope["mac"]): raise SafeError("Relay authentication failed.")
        if expected_nonce is not None:
            if not hmac.compare_digest(nonce, expected_nonce): raise SafeError("Relay response does not match its request.")
        else:
            with self.lock:
                self.seen = {k: v for k, v in self.seen.items() if self.clock() - v <= 65}
                if nonce in self.seen or len(self.seen) >= 10000: raise SafeError("Relay replay rejected.")
                self.seen[nonce] = self.clock()
        return envelope["payload"]

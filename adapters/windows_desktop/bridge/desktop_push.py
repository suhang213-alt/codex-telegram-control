"""Authenticated encrypted cloud-to-desktop delivery; no network or token access.

Production providers are injected by miniapp_server. Tests use synthetic keys,
owners and temporary journals only. Missing enrollment fails closed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from desktop_transport import decode_delivery, receive_delivery, verify_owner, ReceiptError

MAX_BODY_BYTES = 131072
MAX_PLAINTEXT_BYTES = 65536
DEVICE_ID = "desktop-local"
MAX_CLOCK_SKEW = 60


class PushError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64decode(value, size=None, maximum=None):
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PushError("invalid_payload")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise PushError("invalid_payload") from None
    if b64encode(decoded) != value or size is not None and len(decoded) != size or maximum is not None and len(decoded) > maximum:
        raise PushError("invalid_payload")
    return decoded


def parse_json(raw, maximum=MAX_BODY_BYTES):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= maximum:
        raise PushError("request_too_large", 413)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError):
        raise PushError("invalid_payload") from None


def derive_key(master, purpose):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"codex-bridge-push-v1",
                info=purpose.encode("ascii")).derive(master)


def signed(key, *parts):
    return b64encode(hmac.new(key, "\n".join(str(part) for part in parts).encode("utf-8"), hashlib.sha256).digest())


class DesktopPush:
    def __init__(self, *, key_provider, state_provider, process_update, challenge_path,
                 receipt_path, expected_username="example_owner", wake=None, now=time.time):
        self.key_provider, self.state_provider = key_provider, state_provider
        self.process_update, self.challenge_path = process_update, Path(challenge_path)
        self.receipt_path, self.expected_username = Path(receipt_path), expected_username
        self.wake, self.now = wake, now

    def keys(self):
        try:
            value = self.key_provider()
            if not isinstance(value, dict) or set(value) != {"deviceId", "key"} or value["deviceId"] != DEVICE_ID:
                raise ValueError()
            master = b64decode(value["key"], 32)
            return {name: derive_key(master, name) for name in ("challenge-hmac", "delivery-aes-gcm", "receipt-hmac")}
        except Exception:
            raise PushError("push_unavailable", 503) from None

    def validate_envelope(self, value, fields):
        if set(value) != set(fields) or type(value.get("v")) is not int or value["v"] != 1 or value.get("deviceId") != DEVICE_ID:
            raise PushError("invalid_payload")
        timestamp = value.get("timestamp")
        if type(timestamp) is not int or abs(self.now() - timestamp) > MAX_CLOCK_SKEW:
            raise PushError("stale_request", 401)

    def challenge_db(self):
        self.challenge_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.challenge_path), timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS push_challenges (nonce TEXT PRIMARY KEY, timestamp INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)")
        db.commit()
        return db

    def challenge(self, value, keys):
        self.validate_envelope(value, ("v", "deviceId", "timestamp", "nonce", "mac"))
        nonce, timestamp = value["nonce"], value["timestamp"]
        b64decode(nonce, 24)
        actual = b64decode(value["mac"], 32)
        expected = b64decode(signed(keys["challenge-hmac"], "codex-bridge-challenge-v1", DEVICE_ID, timestamp, nonce), 32)
        if not hmac.compare_digest(expected, actual):
            raise PushError("unauthorized", 401)
        db = self.challenge_db()
        try:
            with db:
                db.execute("DELETE FROM push_challenges WHERE timestamp<?", (int(self.now()) - 180,))
                db.execute("INSERT INTO push_challenges(nonce,timestamp) VALUES(?,?) ON CONFLICT(nonce) DO NOTHING", (nonce, timestamp))
                found = db.execute("SELECT timestamp,consumed FROM push_challenges WHERE nonce=?", (nonce,)).fetchone()
                if found != (timestamp, 0):
                    raise PushError("challenge_replayed", 409)
        finally:
            db.close()
        return {"v": 1, "deviceId": DEVICE_ID, "timestamp": timestamp, "nonce": nonce, "ok": True,
                "mac": signed(keys["challenge-hmac"], "codex-bridge-challenge-ok-v1", DEVICE_ID, timestamp, nonce)}

    def deliver(self, value, keys):
        self.validate_envelope(value, ("v", "deviceId", "timestamp", "challenge", "iv", "ciphertext"))
        nonce, timestamp, iv = value["challenge"], value["timestamp"], value["iv"]
        b64decode(nonce, 24)
        iv_bytes = b64decode(iv, 12)
        cipher = b64decode(value["ciphertext"], maximum=MAX_PLAINTEXT_BYTES + 16)
        aad = f"codex-bridge-delivery-v1\n{DEVICE_ID}\n{timestamp}\n{nonce}\n{iv}".encode("utf-8")
        try:
            plaintext = AESGCM(keys["delivery-aes-gcm"]).decrypt(iv_bytes, cipher, aad)
        except Exception:
            raise PushError("unauthorized", 401) from None
        parsed = parse_json(plaintext, MAX_PLAINTEXT_BYTES)
        delivery = decode_delivery(quote(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), safe=""))
        verify_owner(delivery["update"], self.state_provider(), self.expected_username)
        db = self.challenge_db()
        try:
            with db:
                updated = db.execute("UPDATE push_challenges SET consumed=1 WHERE nonce=? AND consumed=0 AND timestamp>=? AND timestamp<=?",
                                     (nonce, int(self.now()) - MAX_CLOCK_SKEW, int(self.now()) + MAX_CLOCK_SKEW))
                if updated.rowcount != 1:
                    raise PushError("challenge_replayed", 409)
        finally:
            db.close()
        result = receive_delivery(delivery, ledger_path=self.receipt_path, process_update=self.process_update, now=self.now())
        if result.get("ok") is not True or result.get("acknowledge") is not True or result.get("disposition") not in ("accepted", "deduplicated"):
            raise PushError("delivery_uncertain", 409)
        if self.wake:
            try:
                notified = self.wake(delivery["requestId"])
                if isinstance(notified, dict) and notified.get("ok") is False:
                    raise RuntimeError()
            except Exception:
                # Receipt stays durable; retry with a new challenge can retry wake.
                raise PushError("wake_unavailable", 503) from None
        disposition = result["disposition"]
        return {"v": 1, "deviceId": DEVICE_ID, "requestId": delivery["requestId"], "claimToken": delivery["claimToken"],
                "challenge": nonce, "iv": iv, "disposition": disposition, "acknowledge": True,
                "mac": signed(keys["receipt-hmac"], "codex-bridge-receipt-v1", DEVICE_ID, delivery["requestId"], delivery["claimToken"], nonce, iv, disposition, "true")}

    def handle(self, path, raw):
        try:
            keys = self.keys()
            value = parse_json(raw)
            if path == "/api/bridge-challenge":
                result = self.challenge(value, keys)
            elif path == "/api/bridge-deliver":
                result = self.deliver(value, keys)
            else:
                raise PushError("not_found", 404)
            return 200, result
        except PushError as error:
            return error.status, {"ok": False, "error": error.code}
        except ReceiptError as error:
            code = error.code if error.code in ("owner_mismatch", "delivery_conflict", "invalid_input") else "delivery_unavailable"
            return 409, {"ok": False, "error": code}
        except Exception:
            return 503, {"ok": False, "error": "push_unavailable"}

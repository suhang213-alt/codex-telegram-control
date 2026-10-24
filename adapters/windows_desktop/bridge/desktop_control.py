"""Encrypted outbound registration and recovery for immediate cloud push.

Commands normally arrive on the authenticated push endpoint. This client
renews the online lease and recovers missed deliveries every 45 seconds.
It never exposes a service bearer or Telegram token to the public gateway.
"""
from __future__ import annotations
import bridge_config as config

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from desktop_push import b64encode, b64decode, derive_key, parse_json, DEVICE_ID, MAX_BODY_BYTES
from desktop_transport import decode_delivery, miniapp_status, receive_delivery, verify_owner

GATEWAY_URL = ""
INTERVAL_SECONDS = 45
TIMEOUT_SECONDS = 5
MARKER = config.data_dir() / ".desktop_control_enabled"


class ControlError(Exception):
    def __init__(self, code):
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ControlError("redirect_rejected")


def post_encrypted(body):
    request = urllib.request.Request(GATEWAY_URL, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    # Do not give a configured proxy ciphertext or permit endpoint redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ControlError("gateway_unavailable")
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise ControlError("response_too_large")
            return body
    except ControlError:
        raise
    except urllib.error.HTTPError as error:
        raise ControlError("gateway_http_" + str(error.code)) from None
    except Exception:
        raise ControlError("gateway_unavailable") from None


class DesktopControl:
    def __init__(self, *, key_provider, panel_provider, receive, wake, journal_path,
                 post=post_encrypted, now=time.time, enabled=lambda: True):
        self.key_provider, self.panel_provider = key_provider, panel_provider
        self.receive, self.wake, self.journal_path = receive, wake, Path(journal_path)
        self.post, self.now = post, now
        self.enabled = enabled
        self.event = threading.Event()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.last_panel = object()

    @contextmanager
    def transaction(self):
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.journal_path), timeout=5)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), claim TEXT, ack TEXT, last_at INTEGER, last_error TEXT)")
            db.execute("INSERT OR IGNORE INTO control VALUES(1,NULL,NULL,NULL,NULL)")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def call(self, payload):
        try:
            enrollment = self.key_provider()
            if not isinstance(enrollment, dict) or set(enrollment) != {"deviceId", "key"} or enrollment["deviceId"] != DEVICE_ID:
                raise ValueError()
            key = derive_key(b64decode(enrollment["key"], 32), "control-aes-gcm")
            timestamp = int(self.now())
            nonce, iv = b64encode(secrets.token_bytes(24)), b64encode(secrets.token_bytes(12))
            aad = f"codex-bridge-control-v1\n{DEVICE_ID}\n{timestamp}\n{nonce}\n{iv}".encode()
            plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            envelope = {"v": 1, "deviceId": DEVICE_ID, "timestamp": timestamp, "nonce": nonce, "iv": iv,
                "ciphertext": b64encode(AESGCM(key).encrypt(b64decode(iv, 12), plaintext, aad))}
            body = json.dumps(envelope, separators=(",", ":")).encode()
            if len(body) > MAX_BODY_BYTES:
                raise ControlError("request_too_large")
            response = parse_json(self.post(body))
            if (set(response) != set(envelope) or type(response.get("v")) is not int or response["v"] != 1
                    or response.get("deviceId") != DEVICE_ID or type(response.get("timestamp")) is not int
                    or response["timestamp"] != timestamp or response.get("nonce") != nonce):
                raise ControlError("invalid_response")
            response_iv = response["iv"]
            response_aad = f"codex-bridge-control-response-v1\n{DEVICE_ID}\n{timestamp}\n{nonce}\n{response_iv}".encode()
            decoded = AESGCM(key).decrypt(b64decode(response_iv, 12),
                b64decode(response["ciphertext"], maximum=MAX_BODY_BYTES), response_aad)
            value = parse_json(decoded)
            if value.get("ok") is not True:
                raise ControlError("control_rejected")
            return value
        except ControlError:
            raise
        except Exception:
            raise ControlError("invalid_response_or_enrollment") from None

    def pending_claim(self):
        with self.transaction() as db:
            claim, ack = db.execute("SELECT claim,ack FROM control").fetchone()
            if claim is None:
                claim = secrets.token_urlsafe(24)
                db.execute("UPDATE control SET claim=? WHERE singleton=1", (claim,))
        return claim, json.loads(ack) if ack else None

    def finish_ack(self, claim, deliveries):
        try:
            value = self.call({"action": "ack", "deliveries": deliveries})
        except ControlError as error:
            if error.code != "gateway_http_409":
                raise
            # Another successful push may have replaced an expired lease and
            # acknowledged it already. Reacquire through FIFO; the shared
            # receipt journal suppresses repeated command effects.
            with self.transaction() as db:
                db.execute("UPDATE control SET claim=NULL,ack=NULL WHERE singleton=1 AND claim=?", (claim,))
            return False
        expected = [delivery["requestId"] for delivery in deliveries]
        if value.get("acknowledged") != expected:
            raise ControlError("ack_unconfirmed")
        with self.transaction() as db:
            db.execute("UPDATE control SET claim=NULL,ack=NULL WHERE singleton=1 AND claim=?", (claim,))
        return True

    def recover_one(self, *, reacquired=False):
        claim, pending_ack = self.pending_claim()
        if pending_ack:
            if self.finish_ack(claim, pending_ack):
                return {"accepted": 0, "acknowledged": len(pending_ack)}
            if reacquired:
                raise ControlError("ack_claim_conflict")
            return self.recover_one(reacquired=True)
        response = self.call({"action": "pull", "claimToken": claim, "limit": 1})
        deliveries = response.get("deliveries")
        if not isinstance(deliveries, list) or len(deliveries) > 1:
            raise ControlError("invalid_delivery")
        if not deliveries:
            with self.transaction() as db:
                db.execute("UPDATE control SET claim=NULL WHERE singleton=1 AND claim=?", (claim,))
            return {"accepted": 0, "acknowledged": 0}
        value = deliveries[0]
        if not isinstance(value, dict) or value.get("claimToken") != claim:
            raise ControlError("invalid_delivery")
        delivery = decode_delivery(quote(json.dumps({name: value.get(name) for name in ("requestId", "update", "claimToken")}), safe=""))
        result = self.receive(delivery)
        if result.get("ok") is not True or result.get("acknowledge") is not True or result.get("disposition") not in {"accepted", "deduplicated"}:
            raise ControlError("delivery_uncertain")
        notified = self.wake(delivery["requestId"])
        if isinstance(notified, dict) and notified.get("ok") is False:
            raise ControlError("wake_unavailable")
        acknowledgement = [{"requestId": delivery["requestId"], "claimToken": claim}]
        with self.transaction() as db:
            db.execute("UPDATE control SET ack=? WHERE singleton=1 AND claim=?",
                       (json.dumps(acknowledgement, separators=(",", ":")), claim))
        acknowledged = self.finish_ack(claim, acknowledgement)
        return {"accepted": 1, "acknowledged": int(acknowledged)}

    def sync(self):
        if not self.enabled():
            return {"ok": True, "enabled": False}
        # The lock also protects manual calls in the same long-lived service.
        if not self.lock.acquire(blocking=False):
            return {"ok": True, "busy": True}
        try:
            panel = self.panel_provider()
            self.call({"action": "register", "panelUrl": panel})
            result = self.recover_one()
            with self.transaction() as db:
                db.execute("UPDATE control SET last_at=?,last_error=NULL WHERE singleton=1", (int(self.now()),))
            return {"ok": True, **result}
        except Exception as error:
            code = error.code if isinstance(error, ControlError) else "control_unavailable"
            try:
                with self.transaction() as db:
                    db.execute("UPDATE control SET last_error=? WHERE singleton=1", (code,))
            except Exception:
                pass
            return {"ok": False, "error": code}
        finally:
            self.lock.release()

    def panel_changed(self):
        try:
            panel = self.panel_provider()
        except Exception:
            return
        if panel != self.last_panel:
            self.last_panel = panel
            self.event.set()

    def run(self):
        self.event.set()
        while not self.stop_event.is_set():
            self.event.wait(INTERVAL_SECONDS)
            self.event.clear()
            if self.stop_event.is_set():
                break
            self.sync()

    def status(self):
        if not self.journal_path.is_file():
            return {"ok": True, "registered": False, "lastSuccessAt": None, "error": None, "ackPending": False}
        db = sqlite3.connect(self.journal_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            last_at, error, ack = db.execute("SELECT last_at,last_error,ack FROM control").fetchone()
        finally:
            db.close()
        return {"ok": True, "registered": last_at is not None and 0 <= self.now() - last_at < 180,
                "lastSuccessAt": last_at, "error": error, "ackPending": ack is not None}


def create_client(*, background_wake=True):
    import telegram_bridge as bridge
    from wake_dispatcher import notify_pending
    from cloud_transport import is_enabled
    def panel():
        path = bridge.APP_DIR / "miniapp_status.bin"
        return miniapp_status(lambda: bridge.read_json(path) if path.is_file() else {})["url"]
    def receive(delivery):
        verify_owner(delivery["update"], bridge.state(), bridge.EXPECTED_USERNAME)
        return receive_delivery(delivery, ledger_path=bridge.SECRET_DIR / "desktop_receipts.sqlite3",
            process_update=lambda update: bridge.process_update(update, acknowledge_callback=False))
    return DesktopControl(key_provider=lambda: bridge.read_json(bridge.SECRET_DIR / "desktop_push_key.bin"),
        panel_provider=panel, receive=receive,
        wake=lambda request_id: notify_pending(request_id, background=background_wake),
        journal_path=bridge.APP_DIR / "desktop_control.sqlite3",
        enabled=lambda: is_enabled() and MARKER.is_file())


_CLIENT = None
_START_LOCK = threading.Lock()
_PANEL_LOCK = threading.Lock()
_LAST_PANEL = object()


def start_background():
    global _CLIENT
    import telegram_bridge as bridge
    from cloud_transport import is_enabled
    if not MARKER.is_file() or not is_enabled() or not (bridge.SECRET_DIR / "desktop_push_key.bin").is_file():
        return False
    with _START_LOCK:
        if _CLIENT is None:
            _CLIENT = create_client()
            threading.Thread(target=_CLIENT.run, name="DesktopPushRegistration", daemon=True).start()
    return True


def notify_panel_changed():
    global _LAST_PANEL
    # This event path uses the supported Codex queue + native MCP. It works
    # even while outbound Sites HTTP is disabled by the separate marker.
    try:
        import telegram_bridge as bridge
        from wake_dispatcher import notify_control_change
        path = bridge.APP_DIR / "miniapp_status.bin"
        panel = miniapp_status(lambda: bridge.read_json(path) if path.is_file() else {})["url"]
        with _PANEL_LOCK:
            if panel != _LAST_PANEL:
                result = notify_control_change()
                if result.get("ok") and result.get("enabled") is True:
                    _LAST_PANEL = panel
    except Exception:
        pass
    if _CLIENT is not None:
        _CLIENT.panel_changed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "sync", "on", "off"])
    args = parser.parse_args()
    try:
        if args.action == "on":
            MARKER.touch(exist_ok=True)
        elif args.action == "off":
            MARKER.unlink(missing_ok=True)
        client = create_client(background_wake=False)
        result = client.sync() if args.action == "sync" else client.status()
        result["enabled"] = MARKER.is_file()
    except Exception:
        result = {"ok": False, "error": "control_unavailable"}
    print(json.dumps(result, separators=(",", ":")))
    raise SystemExit(0 if result.get("ok") else 2)

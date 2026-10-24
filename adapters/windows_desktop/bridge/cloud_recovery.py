"""Recover the durable cloud queue on startup/connection events, without chat jobs."""
from __future__ import annotations
import bridge_config as config

import json
import math
from pathlib import Path
import threading
import time
from urllib.parse import quote

from desktop_control import ControlError, DesktopControl
from desktop_transport import decode_delivery, miniapp_status, receive_delivery, verify_owner

MARKER = config.data_dir() / ".cloud_recovery_enabled"
QUARANTINE = {"delivery_uncertain", "invalid_delivery", "invalid_queue_state"}
AUTH_ERRORS = {"cloud_mcp_server_input_required",
               "cloud_mcp_ephemeral_context_unconfirmed",
               "mcp_tool_rejected"}


class RecoveryClient(DesktopControl):
    """Transport retries retain stable claims; uncertain effects never replay."""

    def __init__(self, *args, network_factory=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.network_factory = network_factory
        self.network_events_active = False
        self.network_events_error = None

    def finish_transport(self):
        pass

    def check_quarantine(self):
        with self.transaction() as db:
            error = db.execute("SELECT last_error FROM control").fetchone()[0]
        if error in QUARANTINE:
            raise ControlError(error)

    def recover_one(self):
        self.check_quarantine()
        claim, pending_ack = self.pending_claim()
        if pending_ack:
            for delivery in pending_ack:
                notified = self.wake(delivery["requestId"])
                if isinstance(notified, dict) and notified.get("ok") is False:
                    raise ControlError("wake_unavailable")
            acknowledged = self.finish_ack(claim, pending_ack)
            return {"queueState": "progress", "accepted": 0, "acknowledged": int(acknowledged)}
        value = self.call({"action": "pull", "claimToken": claim, "limit": 1})
        deliveries = value.get("deliveries")
        state = value.get("queueState")
        if not isinstance(deliveries, list) or len(deliveries) > 1:
            raise ControlError("invalid_delivery")
        if state not in {"ready", "empty", "busy", "attention"} or bool(deliveries) != (state == "ready"):
            raise ControlError("invalid_queue_state")
        if state == "empty":
            with self.transaction() as db:
                db.execute("UPDATE control SET claim=NULL WHERE singleton=1 AND claim=?", (claim,))
            return {"queueState": state, "accepted": 0, "acknowledged": 0}
        if state == "busy":
            retry_at = value.get("retryAt")
            if (type(retry_at) not in (int, float) or not math.isfinite(retry_at)
                    or retry_at < 0):
                raise ControlError("invalid_queue_state")
            return {"queueState": state, "retryAfter": max(1, min(300, retry_at / 1000 - self.now())),
                    "accepted": 0, "acknowledged": 0}
        if state == "attention":
            return {"queueState": state, "accepted": 0, "acknowledged": 0}
        item = deliveries[0]
        if not isinstance(item, dict) or item.get("claimToken") != claim:
            raise ControlError("invalid_delivery")
        delivery = decode_delivery(quote(json.dumps({
            name: item.get(name) for name in ("requestId", "update", "claimToken")
        }), safe=""))
        result = self.receive(delivery)
        if (result.get("ok") is not True or result.get("acknowledge") is not True
                or result.get("disposition") not in {"accepted", "deduplicated"}):
            raise ControlError("delivery_uncertain")
        # Store the ACK before waking. A crash or failed wake cannot replay work.
        acknowledgement = [{"requestId": delivery["requestId"], "claimToken": claim}]
        with self.transaction() as db:
            db.execute("UPDATE control SET ack=? WHERE singleton=1 AND claim=?",
                       (json.dumps(acknowledgement, separators=(",", ":")), claim))
        notified = self.wake(delivery["requestId"])
        if isinstance(notified, dict) and notified.get("ok") is False:
            raise ControlError("wake_unavailable")
        acknowledged = self.finish_ack(claim, acknowledgement)
        return {"queueState": "progress", "accepted": 1, "acknowledged": int(acknowledged)}

    def sync(self, *, max_items=50):
        if not self.enabled():
            return {"ok": True, "enabled": False, "queueState": "disabled"}
        if not self.lock.acquire(blocking=False):
            return {"ok": True, "queueState": "busy", "retryAfter": 1}
        accepted = acknowledged = 0
        try:
            # A parked operation must not reach registration or panel access.
            self.check_quarantine()
            self.call({"action": "registerRecovery", "panelUrl": self.panel_provider()})
            for _ in range(max_items):
                if self.stop_event.is_set():
                    return {"ok": True, "queueState": "stopped"}
                result = self.recover_one()
                accepted += result.get("accepted", 0)
                acknowledged += result.get("acknowledged", 0)
                if result["queueState"] != "progress":
                    break
            else:
                result = {"queueState": "more", "retryAfter": 0}
            with self.transaction() as db:
                existing = db.execute("SELECT last_error FROM control").fetchone()[0]
                if existing in QUARANTINE:
                    raise ControlError(existing)
                db.execute("UPDATE control SET last_at=?,last_error=? WHERE singleton=1",
                           (int(self.now()), "cloud_queue_attention" if result["queueState"] == "attention" else None))
            return {"ok": True, **result, "accepted": accepted, "acknowledged": acknowledged}
        except Exception as error:
            code = error.code if isinstance(error, ControlError) else "recovery_unavailable"
            with self.transaction() as db:
                # Preserve a quarantine written by another client while this
                # request was in flight; transient failures cannot release it.
                existing = db.execute("SELECT last_error FROM control").fetchone()[0]
                if existing in QUARANTINE:
                    code = existing
                db.execute("UPDATE control SET last_error=? WHERE singleton=1", (code,))
            return {"ok": False, "error": code, "accepted": accepted, "acknowledged": acknowledged}
        finally:
            try:
                self.finish_transport()
            finally:
                self.lock.release()

    def run(self):
        subscription = None
        try:
            if self.network_factory is not None:
                try:
                    subscription = self.network_factory(self.event).start()
                    self.network_events_active = True
                except Exception:
                    self.network_events_error = "network_events_unavailable"
            self.run_events()
        finally:
            if subscription is not None:
                try:
                    subscription.close()
                except Exception:
                    self.network_events_error = "network_events_cancel_failed"
                self.network_events_active = False

    def run_events(self):
        retry_after = None
        failures = 0
        self.event.set()
        while not self.stop_event.is_set():
            self.event.wait(retry_after)
            self.event.clear()
            if self.stop_event.is_set():
                break
            result = self.sync()
            if result.get("ok"):
                failures = 0
                retry_after = result.get("retryAfter") if result.get("queueState") in {"busy", "more"} else None
            elif result.get("error") in QUARANTINE | AUTH_ERRORS:
                retry_after = None
            else:
                # Retry only unfinished transport recovery, never a chat/model poll.
                retry_after = min(300, 5 * (2 ** min(failures, 6)))
                failures += 1

    def status(self):
        value = super().status()
        # An event-driven client is intentionally silent after an empty queue;
        # an old timestamp is not proof that the connection is broken.
        value.pop("registered", None)
        value["mode"] = "event_recovery"
        value["enabled"] = self.enabled()
        if getattr(self, "thread", None) is not None:
            value["running"] = self.thread.is_alive()
            value["networkEventsActive"] = self.network_events_active
            value["networkEventsError"] = self.network_events_error
        return value


class McpRecoveryClient(RecoveryClient):
    """Use the owner's existing Codex Apps connection in ephemeral context."""
    def __init__(self, *args, mcp_factory, **kwargs):
        super().__init__(*args, **kwargs)
        self.mcp_factory = mcp_factory
        self.mcp = None

    def call(self, payload):
        from cloud_mcp_client import CloudMcpError
        from desktop_push import DEVICE_ID
        try:
            if self.mcp is None:
                self.mcp = self.mcp_factory()
                self.mcp.start()
            request = dict(payload, deviceId=DEVICE_ID)
            if request["action"] == "registerRecovery":
                request["action"] = "register"
            result = self.mcp.desktop_sync(request, owner_authorized=True)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ControlError("mcp_tool_rejected")
            return result
        except CloudMcpError as error:
            code = "gateway_http_409" if error.code == "claim_conflict" else error.code
            raise ControlError(code) from None

    def finish_transport(self):
        client, self.mcp = self.mcp, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


_CLIENT = None
_LOCK = threading.Lock()


def create_client():
    import telegram_bridge as bridge
    from cloud_transport import is_enabled
    from wake_dispatcher import notify_pending
    from cloud_mcp_client import CloudMcpClient
    from network_events import NetworkChangeEvents
    def panel():
        path = bridge.APP_DIR / "miniapp_status.bin"
        return miniapp_status(lambda: bridge.read_json(path) if path.is_file() else {})["url"]
    def receive(delivery):
        verify_owner(delivery["update"], bridge.state(), bridge.EXPECTED_USERNAME)
        return receive_delivery(delivery, ledger_path=bridge.SECRET_DIR / "desktop_receipts.sqlite3",
                                process_update=lambda update: bridge.process_update(update, acknowledge_callback=False))
    return McpRecoveryClient(
        key_provider=lambda: None,
        panel_provider=panel, receive=receive,
        wake=lambda request_id: notify_pending(request_id),
        journal_path=bridge.APP_DIR / "cloud_recovery.sqlite3",
        enabled=lambda: is_enabled() and MARKER.is_file(),
        mcp_factory=CloudMcpClient, network_factory=NetworkChangeEvents,
    )


def start_background():
    global _CLIENT
    if not MARKER.is_file():
        return False
    with _LOCK:
        if _CLIENT is None:
            _CLIENT = create_client()
            _CLIENT.thread = threading.Thread(target=_CLIENT.run, name="CloudEventRecovery", daemon=True)
            _CLIENT.thread.start()
    return True


def notify_panel_changed():
    if _CLIENT is not None:
        _CLIENT.panel_changed()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "sync"])
    action = parser.parse_args().action
    try:
        client = create_client()
        result = client.status() if action == "status" else client.sync()
        print(json.dumps(result, separators=(",", ":")))
    except Exception:
        print('{"ok":false,"error":"recovery_unavailable"}')
        raise SystemExit(1)

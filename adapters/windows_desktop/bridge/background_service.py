"""Background Windows coordinator for the authenticated Telegram owner queue.

No chat prompt, heartbeat automation or model is used by this process.
"""
from __future__ import annotations
import bridge_config as config

import json
import os
from pathlib import Path
import threading
import time

from background_dispatcher import Dispatcher, Deferred, encoded
from background_events import BackgroundEvent, notify
from native_discovery import discover_environment
from native_mcp_client import NativeMcpClient, SERVICE_THREAD_ID
from operation_ledger import BackgroundLedger

ROOT = Path(__file__).resolve().parent
MARKER = config.data_dir() / ".background_native_enabled"
LOCAL_DIR = config.data_dir()
STATUS_PATH = LOCAL_DIR / "background_status.json"


def enabled():
    return MARKER.is_file() and config.settings().get("compatibility_acknowledged") is True


class BridgeFacade:
    """Only the bridge's existing authenticated, durable accessors are used."""
    def __init__(self, bridge):
        self.bridge = bridge

    def __getattr__(self, name):
        return getattr(self.bridge, name)

    def register_created_work(self, thread_id):
        from work_scope import register, PROVENANCE
        register(self.bridge.APP_DIR / "work_scope",
                 {"threadId": thread_id, "provenance": PROVENANCE})

    def publish_native_question(self, row, event):
        if not event.get("openDecision"):
            raise Deferred("question_no_longer_open")
        questions = event.get("questions") or []
        source = event.get("questionSource")
        if source not in {"native_async", "explicit_text", "native_sync"} or not questions:
            raise Deferred("native_question_not_representable")
        prepared = []
        pending_indices = event.get("pendingIndices", list(range(len(questions))))
        if (not isinstance(pending_indices, list) or not pending_indices or
                any(not isinstance(index, int) or isinstance(index, bool) or
                    index < 0 or index >= len(questions) for index in pending_indices)):
            raise Deferred("question_no_longer_open")
        for index in pending_indices:
            question = questions[index]
            if not isinstance(question, dict):
                raise Deferred("native_question_not_representable")
            text = (event.get("text") if source == "explicit_text" else
                    question.get("title") if source == "native_async" else
                    question.get("question"))
            if not isinstance(text, str) or not text:
                raise Deferred("native_question_not_representable")
            raw_options = question.get("options") or []
            if not isinstance(raw_options, list):
                raise Deferred("native_question_option_unavailable")
            options = []
            for item in raw_options:
                if isinstance(item, str) and item:
                    options.append({"label": item, "description": ""})
                elif isinstance(item, dict) and isinstance(item.get("label"), str) and item["label"]:
                    description = item.get("description", "")
                    if not isinstance(description, str):
                        raise Deferred("native_question_option_unavailable")
                    options.append({"label": item["label"], "description": description})
                else:
                    raise Deferred("native_question_option_unavailable")
            payload = {
                "threadId": row["id"], "itemId": event["itemId"],
                "title": row.get("title") or "Чат", "question": text,
                "options": options, "questionSource": source, "questionIndex": index,
            }
            if source == "native_async":
                payload["questionItemId"] = json.dumps(
                    ["request_user_input_async", event["itemId"], index],
                    ensure_ascii=False, separators=(",", ":"))
            if source == "native_sync":
                payload["isSecret"] = question.get("isSecret") is True
                if isinstance(question.get("header"), str):
                    payload["header"] = question["header"]
            prepared.append(payload)
        for payload in prepared:
            self.bridge.register_decision(encoded(payload))


class BackgroundService:
    def __init__(self, bridge, *, root=ROOT, local_dir=LOCAL_DIR):
        self.root, self.local_dir = Path(root), Path(local_dir)
        self.bridge = BridgeFacade(bridge)
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.event = None
        self.lifecycle = None
        self.directory = None
        self.native = None
        self.dispatcher = None
        self.readiness_until = time.monotonic() + 25
        self.last_error = None
        self.startup_error = None

    def status(self, **values):
        self.local_dir.mkdir(parents=True, exist_ok=True)
        record = {"enabled": enabled(),
                  "processId": os.getpid(), "updatedAt": int(time.time()),
                  "nativeConnected": self.native is not None,
                  "lastError": self.last_error,
                  "pendingErrors": len(self.dispatcher.errors) if self.dispatcher else 0,
                  "activeWatches": len(self.dispatcher.watches) if self.dispatcher else 0,
                  "eventWaitOnly": self.dispatcher.event_wait_only if self.dispatcher else False,
                  **values}
        temporary = self.local_dir / "background_status.tmp"
        temporary.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.local_dir / STATUS_PATH.name)

    def desktop_event(self, event):
        if event.kind in {"desktop_window_created", "desktop_window_shown"}:
            # Bounded readiness following a real OS launch event, never a monitor timer.
            self.readiness_until = time.monotonic() + 25
        if self.event:
            self.event.set()

    def connect(self):
        environment = discover_environment(caller_thread_id=SERVICE_THREAD_ID)
        client = NativeMcpClient(caller_thread_id=SERVICE_THREAD_ID, environment=environment)
        try:
            client.start()
            # A live native request verifies the selected Desktop endpoint.
            probe = client.call("list_threads", {"limit": 1})
            if probe.get("isError"):
                raise RuntimeError("native_identity_probe_failed")
        except Exception:
            client.close()
            raise
        self.native = client

    def start(self):
        self.thread = threading.Thread(target=self.run, name="TelegramNativeBackground", daemon=True)
        self.thread.start()
        if not self.ready.wait(8):
            raise RuntimeError("background_start_timeout")
        if self.startup_error:
            raise RuntimeError(self.startup_error)
        return self

    def drain_pending(self, *, attempted_ids=None, blocked_activity=None):
        """Publish status and re-snapshot between bounded dispatcher tranches."""
        if attempted_ids is None:
            attempted_ids = set()
        if blocked_activity is None:
            blocked_activity = set()
        while not self.stop.is_set():
            more_ready = self.dispatcher.process_batch(
                attempted_ids=attempted_ids, blocked_activity=blocked_activity)
            self.last_error = None
            self.status()
            if not more_ready:
                break

    def run(self):
        from desktop_lifecycle import DesktopLifecycle
        from response_watch import WindowsDirectoryEvents
        try:
            with BackgroundEvent(self.root) as event, BackgroundLedger(
                    self.local_dir / "operations.sqlite3") as ledger:
                self.event = event
                self.lifecycle = DesktopLifecycle(
                    self.desktop_event, on_error=lambda _error: self._source_error("desktop_lifecycle_failed"))
                self.lifecycle.start()
                # Metadata change events only: no file contents are read here.
                self.directory = WindowsDirectoryEvents(config.codex_home(), event.set)
                self.directory.start()
                # Publish this process before declaring startup ready. A large
                # durable backlog can keep the first batch busy for minutes.
                self.status()
                self.ready.set()
                event.set()  # Reconcile durable work already present before subscriptions.
                attempted_ids, blocked_activity = set(), set()
                while not self.stop.is_set():
                    # A genuine OS event wakes durable input and rollout work.
                    # Active watches never turn this into a polling loop.
                    actual_event = event.wait(None)
                    if self.stop.is_set():
                        break
                    if actual_event:
                        attempted_ids.clear()
                        blocked_activity.clear()
                    if actual_event and self.dispatcher:
                        self.dispatcher.new_event()
                    try:
                        if self.native is None:
                            self.connect()
                            self.dispatcher = Dispatcher(self.bridge, self.native, ledger)
                            attempted_ids.clear()
                            blocked_activity.clear()
                            # The first refresh/batch may be long; expose the
                            # verified native connection before processing it.
                            self.status()
                        # Owner input can use the verified live gateway without
                        # waiting for a global refresh/history reconciliation.
                        self.drain_pending(attempted_ids=attempted_ids,
                                           blocked_activity=blocked_activity)
                        if self.stop.is_set():
                            break
                        if actual_event:
                            self.dispatcher.refresh()
                            observer = getattr(self.bridge, "_ACTIVITY_OBSERVER", None)
                            if observer:
                                observer.sync()
                        self.drain_pending(attempted_ids=attempted_ids,
                                           blocked_activity=blocked_activity)
                        if self.stop.is_set():
                            break
                        if self.dispatcher.watches:
                            self.dispatcher.wait_once(attempted_ids=attempted_ids)
                            self.drain_pending(attempted_ids=attempted_ids,
                                               blocked_activity=blocked_activity)
                    except Exception as exc:
                        self.last_error = getattr(exc, "code", "background_processing_failed")
                        if self.native is not None:
                            self.native.close()
                        self.native, self.dispatcher = None, None
                        self.status()
                        if time.monotonic() < self.readiness_until:
                            # Only after a recent real Desktop startup event.
                            if not self.stop.wait(1):
                                event.set()
        except Exception as exc:
            self.last_error = getattr(exc, "code", "background_service_failed")
            self.startup_error = self.last_error
            self.status()
            self.ready.set()
        finally:
            if self.directory:
                self.directory.close()
            if self.lifecycle:
                self.lifecycle.close()
            if self.native:
                self.native.close()
            self.native = None
            self.status(stopped=True)

    def _source_error(self, code):
        self.last_error = code
        if self.event:
            self.event.set()

    def close(self):
        self.stop.set()
        if self.event:
            self.event.set()
        if self.thread:
            self.thread.join(65)


_SERVICE = None
_LOCK = threading.Lock()


def start_background(bridge):
    global _SERVICE
    if not enabled():
        return False
    with _LOCK:
        if _SERVICE is None or not _SERVICE.thread or not _SERVICE.thread.is_alive():
            _SERVICE = BackgroundService(bridge).start()
    return True


def notify_pending():
    return {"ok": True, "enabled": True, "scheduled": notify(ROOT), "transport": "background_native"}


if __name__ == "__main__":
    # Read-only diagnosis. Starting is owned by the existing Windows service.
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "probe"])
    args = parser.parse_args()
    if args.action == "status":
        value = json.loads(STATUS_PATH.read_text(encoding="utf-8")) if STATUS_PATH.is_file() else {"enabled": enabled(), "running": False}
        print(json.dumps(value, separators=(",", ":")))
    else:
        client = None
        try:
            environment = discover_environment(caller_thread_id=SERVICE_THREAD_ID)
            client = NativeMcpClient(caller_thread_id=SERVICE_THREAD_ID, environment=environment)
            names = client.start()
            result = client.call("list_threads", {"limit": 1})
            print(json.dumps({"ok": not result.get("isError"), "nativeTools": len(names),
                              "messagesSent": 0, "modelTurns": 0}))
        finally:
            if client:
                client.close()

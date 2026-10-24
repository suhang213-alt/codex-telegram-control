"""Transparent, single-engine Desktop stdio gateway.

The GUI still owns the real engine. Owner responses use that same stream and
original request IDs; this process never invents an approval or a resolved ACK.
All ordinary traffic is forwarded unchanged. No transcript is logged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

MAX_FRAME = 16 * 1024 * 1024


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class Gateway:
    def __init__(self, write_engine, write_gui, *, epoch=None, persist=lambda _v: None,
                 publish=lambda _kind, _v: None, clock=time.time,
                 retry_delays=(.25, 1, 5, 15, 30), timer_factory=threading.Timer):
        self.epoch = epoch or uuid.uuid4().hex
        self.write_engine, self.write_gui = write_engine, write_gui
        self.persist, self.publish, self.clock = persist, publish, clock
        self.lock = threading.RLock()
        self.pending, self.rpc, self.operations, self.threads = {}, {}, {}, {}
        self.items = {}
        self.events = queue.Queue()
        self.closed = False
        if not retry_delays or any(type(delay) not in (int, float) or not 0 < delay <= 60 for delay in retry_delays):
            raise ValueError("invalid_publish_retry_delays")
        self.retry_delays, self.timer_factory = tuple(retry_delays), timer_factory
        self.retry_attempts, self.retry_timers, self.published = {}, {}, set()
        self.changed = lambda: None
        self.publisher = threading.Thread(target=self._publish, daemon=True)
        self.publisher.start()

    def _publish(self):
        while True:
            event = self.events.get()
            try:
                if event is None:
                    return
                identity = self._event_identity(event)
                with self.lock:
                    if not self._publishable(event) or identity in self.published:
                        self._cancel_publish_retry(identity)
                        continue
                try:
                    # Never hold the stdio/arbitration lock during bridge I/O.
                    self.publish(*event)
                except Exception:
                    self._retry_publish(event)
                else:
                    with self.lock:
                        self.published.add(identity)
                        self._cancel_publish_retry(identity)
            finally:
                self.events.task_done()

    @staticmethod
    def _event_identity(event):
        kind, envelope = event
        return kind, envelope["request"]["key"]

    def _publishable(self, event):
        if self.closed:
            return False
        kind, envelope = event
        if kind != "request":
            return True  # Closing-state persistence must also survive failure.
        request = envelope["request"]
        from native_gateway_protocol import typed_key
        entry = self.pending.get(typed_key(request["requestId"]))
        return (entry is not None and entry["request"]["key"] == request["key"]
                and entry["state"] == "pending")

    def _cancel_publish_retry(self, identity):
        timer = self.retry_timers.pop(identity, None)
        if timer is not None:
            timer.cancel()
        self.retry_attempts.pop(identity, None)

    def _retry_publish(self, event):
        identity = self._event_identity(event)
        with self.lock:
            if not self._publishable(event) or identity in self.published or identity in self.retry_timers:
                return
            attempt = self.retry_attempts.get(identity, 0)
            self.retry_attempts[identity] = attempt + 1
            delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]

            def retry():
                with self.lock:
                    self.retry_timers.pop(identity, None)
                    # Original typed ID may have been reused, or Desktop may
                    # have answered during backoff. Never resurrect that card.
                    if self._publishable(event) and identity not in self.published:
                        self.events.put(event)
                    else:
                        self.retry_attempts.pop(identity, None)

            timer = self.timer_factory(delay, retry)
            timer.daemon = True
            self.retry_timers[identity] = timer
            timer.start()

    def _save(self):
        self.persist({"epoch": self.epoch, "operations": self.operations})

    @staticmethod
    def _decode(line):
        if len(line) > MAX_FRAME:
            return None
        try:
            value = json.loads(line)
            return value if isinstance(value, dict) else None
        except (ValueError, UnicodeError):
            return None

    def from_gui(self, line):
        """Preserve all GUI requests and let only the first approval response win."""
        from native_gateway_protocol import typed_key
        value = self._decode(line)
        with self.lock:
            if value is not None and "method" not in value and "id" in value:
                try:
                    entry = self.pending.get(typed_key(value["id"]))
                except ValueError:
                    entry = None
                if entry is not None:
                    if entry["state"] != "pending":
                        return  # Telegram or Desktop already answered this exact ID.
                    entry["state"] = "desktop_submitted"
            if value is not None and isinstance(value.get("params"), dict):
                params = value["params"]
                target = params.get("threadId")
                if isinstance(target, str) and value.get("method") in {"thread/resume", "turn/start", "turn/steer"}:
                    if target not in self.threads:
                        self.threads[target] = {}
                        try:
                            self.changed()
                        except OSError:
                            pass  # Discovery metadata must never drop GUI input.
            self.write_engine(line)

    def from_engine(self, line):
        from native_gateway_protocol import normalize_request, typed_key
        value = self._decode(line)
        with self.lock:
            if value is not None and "method" not in value and "id" in value:
                own = self.rpc.get(value["id"]) if isinstance(value["id"], str) else None
                if own is not None:
                    own["response"] = value
                    operation = self.operations.get(own.get("operation_id"))
                    if operation is not None:
                        result = self._input_result(own, value)
                        operation.update(state=result["status"], result=result)
                        try:
                            self._save()
                        except OSError:
                            pass  # The real ACK must still reach the waiting client.
                    own["ready"].set()
                    return  # Internal RPC results do not belong to the GUI.
            if value is not None:
                method, params = value.get("method"), value.get("params") or {}
                if isinstance(params, dict):
                    target = params.get("threadId")
                    if isinstance(target, str):
                        newly_discovered = target not in self.threads
                        thread = self.threads.setdefault(target, {})
                        if method == "turn/started":
                            turn = params.get("turn") or {}
                            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                                thread.update(turnId=turn["id"], active=True)
                        elif method == "turn/completed":
                            thread["active"] = False
                        elif method in {"item/started", "item/completed"}:
                            item = params.get("item") or {}
                            if isinstance(item, dict) and isinstance(item.get("id"), str):
                                self.items[(target, params.get("turnId"), item["id"])] = item
                        if newly_discovered:
                            try:
                                self.changed()
                            except OSError:
                                pass  # Discovery must never drop an engine frame.
                    if method == "serverRequest/resolved":
                        try:
                            entry = self.pending.get(typed_key(params.get("requestId")))
                        except ValueError:
                            entry = None
                        if entry is not None and entry["request"]["threadId"] == target:
                            submitted = entry["state"] == "telegram_submitted"
                            entry["state"] = "accepted" if submitted else "closed"
                            entry["ready"].set()
                            operation = self.operations.get(entry.get("operation_id"))
                            if operation is not None:
                                operation.update(state=entry["state"], result={"status": entry["state"],
                                    "request_key": entry["request"]["key"], "threadId": target})
                                try:
                                    self._save()
                                except OSError:
                                    pass
                            self.events.put(("resolved", {"request": entry["request"], "state": entry["state"]}))
                if "id" in value and isinstance(method, str) and isinstance(params, dict):
                    try:
                        request = normalize_request(method, params, value["id"], self.epoch)
                    except (ValueError, TypeError):
                        request = None
                    if request is not None:
                        # Legacy requests use conversationId instead of the
                        # threadId observed in the notification branch above.
                        if request["threadId"] not in self.threads:
                            self.threads[request["threadId"]] = {}
                            try:
                                self.changed()
                            except OSError:
                                pass
                        context = self.items.get((request["threadId"], params.get("turnId"), params.get("itemId")))
                        key = typed_key(value["id"])
                        prior = self.pending.get(key)
                        if prior is None or prior["request"]["key"] != request["key"]:
                            self.pending[key] = {"request": request, "contextItem": context,
                                                 "state": "pending", "ready": threading.Event()}
                            self.events.put(("request", {"request": request, "contextItem": context}))
            self.write_gui(line)

    def status(self):
        return {"epoch": self.epoch, "closed": self.closed, "thread_ids": list(self.threads),
                "pending": sum(e["state"] == "pending" for e in self.pending.values())}

    @staticmethod
    def _input_result(rpc, received):
        if "error" in received:
            return {"status": "rejected"}
        payload = received.get("result")
        if not isinstance(payload, dict):
            return {"status": "uncertain"}
        turn = payload.get("turn") or {}
        turn_id = payload.get("turnId") or (turn.get("id") if isinstance(turn, dict) else None)
        if not isinstance(turn_id, str) or not turn_id or (rpc.get("expectedTurnId") and turn_id != rpc["expectedTurnId"]):
            return {"status": "uncertain"}
        return {"status": "accepted", "threadId": rpc["threadId"], "turnId": turn_id,
                "clientUserMessageId": rpc["clientUserMessageId"]}

    def command(self, message, *, timeout=5):
        from native_gateway_protocol import validate_response
        if not isinstance(message, dict) or message.get("version") != 1:
            raise ValueError("invalid_gateway_command")
        command = message.get("command")
        operation_id = message.get("operation_id")
        with self.lock:
            if self.closed:
                raise ValueError("gateway_disconnected")
            if command == "status":
                return self.status()
            if command == "list_requests":
                target = message.get("thread_id")
                if not isinstance(target, str) or target not in self.threads:
                    raise ValueError("gateway_thread_unavailable")
                return {"requests": [{"request": e["request"], "state": e["state"],
                                      "contextItem": e.get("contextItem")}
                                     for e in self.pending.values() if e["request"]["threadId"] == target]}
            if command not in {"answer", "owner_input"} or not isinstance(operation_id, str) or not operation_id or len(operation_id) > 200:
                raise ValueError("invalid_gateway_operation")
            digest = hashlib.sha256(encoded(message)).hexdigest()
            prior = self.operations.get(operation_id)
            if prior is not None:
                if prior["digest"] != digest:
                    raise ValueError("gateway_operation_conflict")
                return prior.get("result") or {"status": "uncertain"}
            if command == "answer":
                entry = next((e for e in self.pending.values() if e["request"]["key"] == message.get("request_key")), None)
                if entry is None or entry["state"] != "pending":
                    raise ValueError("gateway_request_not_open")
                request = entry["request"]
                if message.get("thread_id") != request["threadId"]:
                    raise ValueError("gateway_thread_mismatch")
                response = message.get("response")
                validate_response(request, response)
                wire = {"id": request["requestId"], "result": response}
                waiter = entry["ready"]
            else:
                target, prompt = message.get("thread_id"), message.get("prompt")
                if target not in self.threads or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
                    raise ValueError("gateway_thread_or_input_unavailable")
                thread = self.threads[target]
                params = {"threadId": target, "input": [{"type": "text", "text": prompt}],
                          "clientUserMessageId": str(uuid.uuid5(uuid.NAMESPACE_URL, self.epoch + operation_id))}
                method = "turn/start"
                if thread.get("active"):
                    if not thread.get("turnId"):
                        raise ValueError("gateway_active_turn_unconfirmed")
                    method = "turn/steer"
                    params["expectedTurnId"] = thread["turnId"]
                rpc_id = "telegram-gateway-" + self.epoch + "-" + uuid.uuid4().hex
                waiter = threading.Event()
                self.rpc[rpc_id] = {"ready": waiter, "operation_id": operation_id,
                                    "threadId": target, "clientUserMessageId": params["clientUserMessageId"],
                                    "expectedTurnId": params.get("expectedTurnId")}
                wire = {"id": rpc_id, "method": method, "params": params}
            self.operations[operation_id] = {"digest": digest, "state": "staged", "at": self.clock()}
            try:
                self._save()  # durable boundary before any external effect
            except Exception:
                self.operations.pop(operation_id, None)
                if command == "owner_input":
                    self.rpc.pop(rpc_id, None)
                raise
            if command == "answer":
                entry.update(state="telegram_submitted", operation_id=operation_id)
            try:
                self.write_engine(encoded(wire))
            except Exception:
                self.operations[operation_id].update(state="uncertain", result={"status": "uncertain"})
                self._save()
                return {"status": "uncertain"}
        waiter.wait(timeout)
        with self.lock:
            operation = self.operations[operation_id]
            if command == "answer":
                result = {"status": "accepted" if entry["state"] == "accepted" else "uncertain",
                          "request_key": request["key"], "threadId": request["threadId"]}
            else:
                received = self.rpc[rpc_id].get("response")
                if received is None:
                    result = {"status": "uncertain"}
                elif "error" in received:
                    result = {"status": "rejected"}
                else:
                    result = self._input_result(self.rpc[rpc_id], received)
            operation.update(state=result["status"], result=result)
            self._save()
            return result

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for timer in self.retry_timers.values():
                timer.cancel()
            self.retry_timers.clear()
            self.retry_attempts.clear()
            for entry in self.pending.values():
                entry["ready"].set()
            for entry in self.rpc.values():
                entry["ready"].set()
            # No outstanding publication should begin after engine shutdown.
            while True:
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    break
                else:
                    self.events.task_done()
            self.events.put(None)


def serve(real_cli, arguments):
    from multiprocessing.connection import Listener
    import telegram_bridge as bridge
    from native_gateway_bridge import publish_event

    real_cli = Path(real_cli).resolve(strict=True)
    environment = dict(os.environ)
    # The hash-checked launcher has already preserved the real engine's exact
    # sandbox identity. Never read an unverified mutable override file here.
    environment["CODEX_CLI_PATH"] = str(real_cli)
    child = None
    engine_lock, gui_lock = threading.Lock(), threading.Lock()
    def write(stream, lock, data):
        with lock:
            stream.write(data); stream.flush()
    epoch = uuid.uuid4().hex
    directory = bridge.APP_DIR / "native_gateway"
    keys = bridge.APP_DIR / "native_gateway_keys"
    directory.mkdir(parents=True, exist_ok=True); keys.mkdir(parents=True, exist_ok=True)
    authkey = os.urandom(32)
    keyfile = keys / (epoch + ".bin")
    keyfile.write_bytes(bridge.protect(authkey))
    journal = directory / (epoch + ".operations.bin")
    def persist(value):
        bridge.atomic_json(journal, value)
    gateway = Gateway(lambda b: write(child.stdin, engine_lock, b),
                      lambda b: write(sys.stdout.buffer, gui_lock, b), epoch=epoch,
                      persist=persist, publish=lambda k,v: publish_event(bridge, k, v))
    address = r"\\.\pipe\codex-native-gateway-" + epoch
    try:
        listener = Listener(address, family="AF_PIPE", authkey=authkey)
    except Exception:
        gateway.close(); keyfile.unlink(missing_ok=True)
        raise
    manifest = directory / (epoch + ".json")
    def metadata():
        data = {"schema": "codex-native-gateway/1", "epoch": epoch, "pid": os.getpid(),
                "engine_pid": child.pid, "pipe": address, "thread_ids": list(gateway.threads),
                "authkey_file": str(keyfile), "at": time.time()}
        temporary = manifest.with_suffix(".tmp")
        temporary.write_bytes(encoded(data)); os.replace(temporary, manifest)
    try:
        child = subprocess.Popen([str(real_cli), *arguments], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=None, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        metadata()
    except Exception:
        # No GUI initialize/input has been relayed at this bootstrap point.
        if child is not None:
            child.stdin.close()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.terminate(); child.wait(timeout=3)
        gateway.close(); listener.close(); keyfile.unlink(missing_ok=True)
        raise
    gateway.changed = metadata
    def handle(connection):
        operation_id = None
        try:
            if not connection.poll(5):
                return
            message = json.loads(connection.recv_bytes(MAX_FRAME))
            operation_id = message.get("operation_id")
            result = gateway.command(message)
            connection.send_bytes(encoded({"ok": True, "operation_id": operation_id, "result": result}))
        except Exception as exc:
            try:
                code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("gateway_") else "gateway_request_failed"
                connection.send_bytes(encoded({"ok": False, "operation_id": operation_id, "error": code,
                                                "uncertain": not isinstance(exc, ValueError)}))
            except Exception:
                pass
        finally:
            connection.close()
    def controls():
        while not gateway.closed:
            try:
                connection = listener.accept()
                threading.Thread(target=handle, args=(connection,), daemon=True).start()
            except Exception:
                if gateway.closed:
                    return
    threading.Thread(target=controls, daemon=True).start()
    def gui_input():
        try:
            for line in sys.stdin.buffer:
                gateway.from_gui(line)
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass
    threading.Thread(target=gui_input, daemon=True).start()
    try:
        for line in child.stdout:
            before = len(gateway.threads)
            gateway.from_engine(line)
            if len(gateway.threads) != before:
                try:
                    metadata()
                except OSError:
                    pass
        return child.wait()
    finally:
        gateway.close(); listener.close()
        # Only this instance's volatile metadata and key are removed.
        manifest.unlink(missing_ok=True); keyfile.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-cli", required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    return serve(args.real_cli, arguments)


if __name__ == "__main__":
    raise SystemExit(main())

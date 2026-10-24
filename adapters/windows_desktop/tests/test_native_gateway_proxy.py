"""Same-engine protocol/arbitration tests. No Desktop, models or network."""
import copy
import json
import threading
import unittest

from native_gateway_protocol import COMMAND, build_response
from native_gateway_proxy import Gateway, encoded


THREAD = "22db506f-36bc-5200-af96-087ee32092c6"
OTHER = "1b566eb1-3d5b-5000-96b7-7be2779da1c3"


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.gui, self.saved = [], [], []
        self.gateway = Gateway(self.engine.append, self.gui.append,
                               persist=lambda value: self.saved.append(copy.deepcopy(value)))
        self.addCleanup(self.gateway.close)

    def request(self, request_id=7, thread=THREAD):
        self.gateway.from_engine(encoded({"id": request_id, "method": COMMAND,
            "params": {"threadId": thread, "turnId": "turn-1", "itemId": "item-1",
                       "reason": "Точный вопрос?", "command": "echo safe", "cwd": "C:/isolated"}}))
        return next(e["request"] for e in self.gateway.pending.values() if e["request"]["threadId"] == thread)

    def answer(self, request, operation="operation-1"):
        return {"version": 1, "command": "answer", "operation_id": operation,
                "thread_id": request["threadId"], "request_key": request["key"],
                "response": build_response(request, choice=0)}

    def resolve(self, request):
        self.gateway.from_engine(encoded({"method": "serverRequest/resolved", "params": {
            "threadId": request["threadId"], "requestId": request["requestId"]}}))

    def test_unrelated_frames_forward_byte_for_byte_in_both_directions(self):
        for raw in (b'{ "method": "initialized" }\r\n', b'{"unknown":"literal \\n"}\n', b'not-json\n'):
            self.gateway.from_gui(raw); self.gateway.from_engine(raw)
        self.assertEqual(self.engine, self.gui)

    def test_native_request_is_forwarded_before_any_owner_answer(self):
        self.request()
        self.assertEqual(len(self.gui), 1)
        self.assertFalse(self.engine)

    def test_new_server_thread_refreshes_discovery_once_including_legacy_request(self):
        discovery = []
        self.gateway.changed = lambda: discovery.append(copy.deepcopy(self.gateway.threads))
        first = encoded({"method": "turn/started", "params": {"threadId": THREAD, "turn": {"id": "new-turn"}}})
        self.gateway.from_engine(first)
        self.assertEqual(discovery[0][THREAD], {"turnId": "new-turn", "active": True})
        self.gateway.from_engine(encoded({"method": "item/started", "params": {"threadId": THREAD, "turnId": "new-turn", "item": {"id": "new-item"}}}))
        self.assertEqual(len(discovery), 1)
        self.gateway.from_engine(encoded({"id": "legacy-new", "method": "execCommandApproval", "params": {
            "conversationId": OTHER, "callId": "legacy-call", "command": ["echo", "fixture"], "cwd": "C:/isolated", "parsedCmd": []}}))
        self.assertEqual(len(discovery), 2)
        self.assertIn(OTHER, discovery[-1])
        self.assertEqual(self.gui[0], first)

    def test_discovery_write_failure_never_drops_engine_notification_or_request(self):
        def unavailable_metadata():
            raise OSError("isolated metadata failure")
        self.gateway.changed = unavailable_metadata
        notification = encoded({"method": "turn/started", "params": {"threadId": THREAD, "turn": {"id": "new-turn"}}})
        self.gateway.from_engine(notification)
        request = self.request(8, OTHER)
        self.assertEqual(self.gui[0], notification)
        self.assertEqual(len(self.gui), 2)
        self.assertIn(THREAD, self.gateway.threads)
        self.assertIn(OTHER, self.gateway.threads)
        self.assertEqual(self.gateway.pending["int:8"]["request"]["key"], request["key"])

    def test_reply_is_durable_before_exact_original_id_is_written_and_real_ack_required(self):
        request = self.request()
        def write(raw):
            self.assertEqual(self.saved[-1]["operations"]["operation-1"]["state"], "staged")
            wire = json.loads(raw); self.assertEqual(wire["id"], 7)
            self.assertEqual(wire["result"], {"decision": "accept"})
            self.engine.append(raw); self.resolve(request)
        self.gateway.write_engine = write
        self.assertEqual(self.gateway.command(self.answer(request))["status"], "accepted")
        self.assertEqual(len(self.gui), 2)

    def test_timeout_never_synthesizes_ack_or_resends(self):
        request = self.request(); message = self.answer(request)
        self.assertEqual(self.gateway.command(message, timeout=0)["status"], "uncertain")
        self.assertEqual(self.gateway.command(message, timeout=0)["status"], "uncertain")
        self.assertEqual(len(self.engine), 1)
        self.assertEqual(len(self.gui), 1)
        self.resolve(request)
        self.assertEqual(self.gateway.command(message)["status"], "accepted")
        self.assertEqual(len(self.engine), 1)

    def test_desktop_response_wins_and_late_telegram_is_rejected(self):
        request = self.request()
        raw = encoded({"id": 7, "result": {"decision": "decline"}})
        self.gateway.from_gui(raw)
        with self.assertRaisesRegex(ValueError, "not_open"):
            self.gateway.command(self.answer(request), timeout=0)
        self.assertEqual(self.engine, [raw])

    def test_telegram_response_wins_and_late_desktop_response_is_suppressed(self):
        request = self.request(); self.gateway.command(self.answer(request), timeout=0)
        self.gateway.from_gui(encoded({"id": 7, "result": {"decision": "acceptForSession"}}))
        self.assertEqual(len(self.engine), 1)

    def test_persistence_failure_leaves_desktop_request_answerable(self):
        request = self.request()
        self.gateway.persist = lambda _value: (_ for _ in ()).throw(OSError("isolated failure"))
        with self.assertRaises(OSError):
            self.gateway.command(self.answer(request), timeout=0)
        self.assertFalse(self.engine)
        raw = encoded({"id": 7, "result": {"decision": "decline"}})
        self.gateway.from_gui(raw); self.assertEqual(self.engine, [raw])

    def test_wrong_thread_stale_key_and_unoffered_choice_write_nothing(self):
        request = self.request(); original = self.answer(request)
        for key, value in (("thread_id", OTHER), ("request_key", "old"), ("response", {"decision": "invented"})):
            message = copy.deepcopy(original); message[key] = value
            with self.assertRaises(ValueError):
                self.gateway.command(message, timeout=0)
        self.assertFalse(self.engine)

    def test_typed_request_ids_and_thread_filter_are_isolated(self):
        self.request(7); self.request("7", OTHER)
        self.assertEqual(len(self.gateway.pending), 2)
        result = self.gateway.command({"version": 1, "command": "list_requests", "thread_id": THREAD})
        self.assertEqual([e["request"]["threadId"] for e in result["requests"]], [THREAD])
        self.assertNotIn(OTHER, json.dumps(result))

    def test_owner_input_steers_same_engine_and_internal_rpc_never_leaks_to_gui(self):
        self.gateway.from_engine(encoded({"method": "turn/started", "params": {
            "threadId": THREAD, "turn": {"id": "turn-1"}}}))
        def write(raw):
            wire = json.loads(raw); self.engine.append(raw)
            self.assertEqual(wire["method"], "turn/steer")
            self.assertEqual(wire["params"]["expectedTurnId"], "turn-1")
            self.assertEqual(wire["params"]["input"], [{"type": "text", "text": "Точный ответ"}])
            self.gateway.from_engine(encoded({"id": wire["id"], "result": {"turnId": "turn-1"}}))
        self.gateway.write_engine = write
        command = {"version": 1, "command": "owner_input", "operation_id": "input-1",
                   "thread_id": THREAD, "prompt": "Точный ответ"}
        result = self.gateway.command(command)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["clientUserMessageId"])
        self.assertEqual(len(self.gui), 1)
        self.assertEqual(self.gateway.command(command), result)
        self.assertEqual(len(self.engine), 1)

    def test_late_owner_input_ack_reconciles_without_repeat(self):
        self.gateway.threads[THREAD] = {"turnId": "t", "active": True}
        command = {"version": 1, "command": "owner_input", "operation_id": "input-1", "thread_id": THREAD, "prompt": "exact"}
        self.assertEqual(self.gateway.command(command, timeout=0)["status"], "uncertain")
        wire = json.loads(self.engine[0])
        self.gateway.from_engine(encoded({"id": wire["id"], "result": {"turnId": "t"}}))
        self.assertEqual(self.gateway.command(command)["status"], "accepted")
        self.assertEqual(len(self.engine), 1)

    def test_cleanup_without_local_submission_is_closed_not_accepted(self):
        request = self.request(); self.resolve(request)
        self.assertEqual(next(iter(self.gateway.pending.values()))["state"], "closed")
        self.assertFalse(self.gateway.operations)

    def test_changed_operation_payload_and_closed_epoch_are_rejected(self):
        request = self.request(); message = self.answer(request)
        self.gateway.command(message, timeout=0)
        message["response"] = {"decision": "decline"}
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.gateway.command(message, timeout=0)
        self.gateway.close()
        with self.assertRaisesRegex(ValueError, "disconnected"):
            self.gateway.command({"version": 1, "command": "status"})


class PublicationRetryTests(unittest.TestCase):
    """Controlled clocks/timers: prove retries without sleeps or real I/O."""

    def setUp(self):
        self.engine, self.gui, self.timers = [], [], []
        self.attempts, self.registered = [], []
        self.failures = {}
        self.blocked, self.release = threading.Event(), threading.Event()
        test = self

        class ManualTimer:
            def __init__(self, delay, callback):
                self.delay, self.callback, self.cancelled = delay, callback, False
                test.timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

            def fire(self):
                # Deliberately allow a callback already racing with cancel;
                # the gateway must independently guard its closed state.
                self.callback()

        def publish(kind, envelope):
            key = kind, envelope["request"]["key"]
            self.attempts.append(key)
            if self.failures.get(kind, 0):
                self.failures[kind] -= 1
                raise OSError("isolated publication failure")
            self.registered.append(key)

        self.gateway = Gateway(self.engine.append, self.gui.append, publish=publish,
                               timer_factory=ManualTimer, retry_delays=(.1, .2))
        self.addCleanup(self.gateway.close)
        self.addCleanup(self.release.set)

    def request(self, request_id=7, item="item-1"):
        frame = encoded({"id": request_id, "method": COMMAND,
            "params": {"threadId": THREAD, "turnId": "turn-1", "itemId": item,
                       "reason": "Точный вопрос?", "command": "echo safe"}})
        self.gateway.from_engine(frame)
        return frame, self.gateway.pending["int:" + str(request_id)]["request"]

    def resolve(self, request):
        self.gateway.from_engine(encoded({"method": "serverRequest/resolved", "params": {
            "threadId": request["threadId"], "requestId": request["requestId"]}}))

    def test_failed_registration_retries_once_and_duplicate_event_is_idempotent(self):
        self.failures["request"] = 1
        _, req = self.request()
        self.gateway.events.join()
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(self.registered, [])
        self.assertEqual(self.timers[0].delay, .1)
        self.timers[0].fire()
        self.gateway.events.join()
        self.assertEqual(self.registered, [("request", req["key"])])
        self.gateway.events.put(("request", {"request": req}))
        self.gateway.events.join()
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.gateway.retry_timers, {})

    def test_backoff_does_not_block_other_events_and_is_capped(self):
        self.failures["request"] = 3
        self.request()
        self.gateway.events.join()
        self.timers[-1].fire(); self.gateway.events.join()
        self.timers[-1].fire(); self.gateway.events.join()
        self.assertEqual([timer.delay for timer in self.timers], [.1, .2, .2])
        _, second = self.request(8, "item-2")
        self.gateway.events.join()
        self.assertIn(("request", second["key"]), self.registered)

    def test_slow_publisher_never_holds_stdio_forwarding_lock(self):
        def blocked_publish(_kind, _envelope):
            self.blocked.set()
            self.release.wait(2)
        self.gateway.publish = blocked_publish
        first, _ = self.request()
        self.assertTrue(self.blocked.wait(1))
        other = b'{"method":"unrelated/notification","params":{}}\n'
        self.gateway.from_engine(other)
        self.assertEqual(self.gui, [first, other])
        self.assertFalse(self.release.is_set())
        self.release.set()
        self.gateway.events.join()

    def test_desktop_resolution_before_retry_never_resurrects_request(self):
        self.failures["request"] = 1
        _, req = self.request()
        self.gateway.events.join()
        retry = self.timers[0]
        self.resolve(req)
        self.gateway.events.join()
        retry.fire(); self.gateway.events.join()
        self.assertEqual([kind for kind, _ in self.attempts], ["request", "resolved"])
        self.assertEqual(self.registered, [("resolved", req["key"])])

    def test_reused_rpc_id_does_not_publish_old_payload(self):
        self.failures["request"] = 1
        _, old = self.request()
        self.gateway.events.join()
        retry = self.timers[0]
        _, current = self.request(item="replacement-item")
        self.gateway.events.join()
        retry.fire(); self.gateway.events.join()
        self.assertNotEqual(old["key"], current["key"])
        self.assertEqual(self.registered, [("request", current["key"])])

    def test_resolved_store_failure_retries_even_when_request_is_closed(self):
        _, req = self.request()
        self.gateway.events.join()
        self.failures["resolved"] = 1
        self.resolve(req)
        self.gateway.events.join()
        self.assertEqual(self.gateway.pending["int:7"]["state"], "closed")
        self.timers[-1].fire(); self.gateway.events.join()
        self.assertEqual(self.registered, [("request", req["key"]), ("resolved", req["key"])])

    def test_close_cancels_timer_and_prevents_racing_retry_queue(self):
        self.failures["request"] = 1
        self.request()
        self.gateway.events.join()
        retry = self.timers[0]
        self.gateway.close()
        self.gateway.events.join()
        retry.fire()
        self.assertTrue(retry.cancelled)
        self.assertTrue(self.gateway.events.empty())
        self.assertEqual(len(self.attempts), 1)
        self.assertFalse(self.gateway.retry_timers)


if __name__ == "__main__":
    unittest.main()

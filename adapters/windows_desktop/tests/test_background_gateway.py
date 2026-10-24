"""Owner text uses the same engine; no native fallback after a write."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from background_dispatcher import Dispatcher
from native_gateway_client import GatewayError
from native_mcp_client import NativeError
from operation_ledger import BackgroundLedger

THREAD = "22db506f-36bc-5200-af96-087ee32092c6"


class Bridge:
    def __init__(self):
        self.replies, self.acks = [], []
        self.records = []

    def pending_records(self):
        return [r for r in self.records if str(r["id"]) not in self.acks]

    def cache_snapshot(self, *args):
        pass

    def work_scope_ids(self):
        return set()

    def thread_in_scope(self, target):
        return target == THREAD

    def reply_payload(self, value):
        self.replies.append(value)

    def ack_pending(self, operation):
        self.acks.append(operation)


class Native:
    def __init__(self):
        self.calls = []

    def call(self, name, args, **kwargs):
        self.calls.append((name, copy.deepcopy(args)))
        value = {"threads": [{"id": THREAD, "kind": "codex", "hostId": "local", "status": "idle"}]}
        if name == "read_thread":
            value = {"turns": []}
        return {"content": [{"type": "text", "text": json.dumps(value)}]}


class Gateway:
    def __init__(self, *, available=True, result=None, failure=None):
        self.available, self.result, self.failure = available, result, failure
        self.calls = []

    def discover(self, target):
        if not self.available:
            raise GatewayError("native_gateway_unavailable")
        return object()

    def owner_input(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return self.result or {"status": "accepted", "threadId": THREAD,
                               "turnId": "turn-1", "clientUserMessageId": "client-1"}


class DispatcherGatewayTests(unittest.TestCase):
    def setUp(self):
        allowed = patch("bridge_config.thread_allowed", side_effect=lambda value: value == THREAD)
        allowed.start()
        self.addCleanup(allowed.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = BackgroundLedger(Path(self.temp.name) / "ledger.sqlite")
        self.addCleanup(self.ledger.close)
        self.native, self.bridge = Native(), Bridge()
        self.dispatcher = Dispatcher(self.bridge, self.native, self.ledger)
        self.record = {"id": 100, "kind": "followup", "selected_thread_id": THREAD,
                       "decision_answer": True, "text": "Точный ответ владельца\nбез изменений"}

    def run_with(self, gateway):
        with patch("background_dispatcher.NativeGatewayClient", return_value=gateway):
            self.dispatcher.process(self.record)

    def test_exact_owner_answer_once_has_real_engine_receipt(self):
        gateway = Gateway()
        self.run_with(gateway)
        self.run_with(gateway)
        self.assertEqual(gateway.calls, [{"thread_id": THREAD, "prompt": self.record["text"], "operation_id": "100"}])
        self.assertEqual([name for name, _ in self.native.calls], ["list_threads"])
        receipt = self.ledger.get("100").receipt
        self.assertEqual(receipt["deliveryTransport"], "native_gateway")
        self.assertEqual(receipt["clientUserMessageId"], "client-1")
        self.assertEqual(receipt["turnId"], "turn-1")
        self.assertEqual(self.ledger.get("100").state, "acked")
        self.assertEqual(len(self.bridge.replies), 1)

    def test_unavailable_proxy_retains_existing_route_until_install(self):
        gateway = Gateway(available=False)
        self.run_with(gateway)
        self.assertEqual(gateway.calls, [])
        self.assertEqual([name for name, _ in self.native.calls], ["list_threads", "read_thread", "send_message_to_thread"])

    def test_uncertain_write_never_falls_back_or_replays(self):
        gateway = Gateway(failure=GatewayError("native_gateway_timeout", uncertain=True))
        with self.assertRaises(NativeError):
            self.run_with(gateway)
        self.assertEqual(self.ledger.get("100").state, "uncertain")
        self.run_with(gateway)
        self.assertEqual(len(gateway.calls), 1)
        self.assertNotIn("send_message_to_thread", [name for name, _ in self.native.calls])

    def test_proxy_disappears_after_discovery_does_not_fall_back(self):
        gateway = Gateway(failure=GatewayError("native_gateway_unavailable"))
        with self.assertRaises(NativeError):
            self.run_with(gateway)
        self.assertEqual(self.ledger.get("100").state, "uncertain")
        self.assertNotIn("send_message_to_thread", [name for name, _ in self.native.calls])

    def test_unconfirmed_engine_response_remains_uncertain(self):
        for result in [{"status": "uncertain"}, {"status": "accepted", "threadId": "other"},
                       {"status": "accepted", "threadId": THREAD, "turnId": "turn-1"}]:
            with self.subTest(result=result):
                self.record["id"] += 1
                with self.assertRaises(NativeError):
                    self.run_with(Gateway(result=result))
                self.assertEqual(self.ledger.get(str(self.record["id"])).state, "uncertain")
        self.assertNotIn("send_message_to_thread", [name for name, _ in self.native.calls])

    def test_exact_engine_rejection_is_reported_without_fallback(self):
        self.run_with(Gateway(result={"status": "rejected"}))
        self.assertEqual(self.ledger.get("100").receipt["status"], "rejected")
        self.assertFalse(self.bridge.replies[0]["ok"])
        self.assertNotIn("send_message_to_thread", [name for name, _ in self.native.calls])

    def test_ordinary_followup_is_delivered_to_existing_engine_once(self):
        self.record.pop("decision_answer")
        gateway = Gateway()
        self.run_with(gateway)
        self.run_with(gateway)
        self.assertEqual(gateway.calls, [{"thread_id": THREAD, "prompt": self.record["text"], "operation_id": "100"}])
        self.assertEqual([name for name, _ in self.native.calls], ["list_threads"])
        self.assertEqual(self.ledger.get("100").receipt["deliveryTransport"], "native_gateway")

    def test_plain_followup_uncertain_write_is_not_replayed_or_delegated(self):
        self.record.pop("decision_answer")
        gateway = Gateway(failure=GatewayError("native_gateway_timeout", uncertain=True))
        with self.assertRaises(NativeError):
            self.run_with(gateway)
        self.run_with(gateway)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(self.ledger.get("100").state, "uncertain")
        self.assertNotIn("send_message_to_thread", [name for name, _ in self.native.calls])

    def test_model_and_effort_overrides_are_preserved_by_original_route(self):
        self.record.pop("decision_answer")
        self.record.update(model="explicit-model", thinking="high")
        gateway = Gateway()
        self.run_with(gateway)
        self.assertEqual(gateway.calls, [])
        arguments = next(args for name, args in self.native.calls if name == "send_message_to_thread")
        self.assertEqual(arguments["model"], "explicit-model")
        self.assertEqual(arguments["thinking"], "high")

    def test_attachment_is_not_silently_lost_through_text_only_gateway(self):
        attachment = Path(self.temp.name) / "attachment.txt"
        attachment.write_text("Synthetic attachment", encoding="utf-8")
        self.record.pop("decision_answer")
        self.record["attachments"] = [{"path": str(attachment.resolve())}]
        gateway = Gateway()
        self.run_with(gateway)
        self.assertEqual(gateway.calls, [])
        arguments = next(args for name, args in self.native.calls if name == "send_message_to_thread")
        self.assertIn(str(attachment.resolve()), arguments["prompt"])

    def test_queued_owner_input_precedes_slow_refresh_and_history(self):
        self.bridge.records = [self.record]
        self.dispatcher.rows = {THREAD: {"id": THREAD, "kind": "codex", "hostId": "local"}}
        gateway = Gateway()
        with patch("background_dispatcher.NativeGatewayClient", return_value=gateway):
            self.dispatcher.process_batch()
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.ledger.get("100").state, "acked")
        # No new durable work: active watches alone must not repeat the same
        # global read before/after each event snapshot.
        self.dispatcher.process_batch()
        self.assertEqual(self.native.calls, [])

    def test_wait_snapshot_is_immediate_and_yields_to_new_owner_input(self):
        self.dispatcher.watches = {THREAD: {"row": {"id": THREAD, "kind": "codex", "hostId": "local"}}}
        self.dispatcher.call = Mock(return_value={"polls": []})
        self.dispatcher.wait_once()
        self.assertEqual(self.dispatcher.call.call_args.args[1]["timeoutMs"], 0)
        self.dispatcher.call.reset_mock()
        self.bridge.records = [self.record]
        self.dispatcher.wait_once()
        self.dispatcher.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()

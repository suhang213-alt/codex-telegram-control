"""Isolated owner→Telegram→same live request bridge acceptance contracts.

No IPC/network or live owner data: uses FakeBridge and a strict gateway double.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import decision_service as decisions
import native_gateway_bridge as gateway
import native_gateway_protocol as protocol
from native_gateway_client import GatewayError
from test_decision_service import FakeBridge, THREAD


class GatewayBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = FakeBridge(Path(self.tmp.name))
        self.bridge.cached_threads = lambda: [{"id": THREAD, "title": "Проверочный чат"}]
        self.bridge.local_codex_threads = lambda: []
        self.requests = []
        self.calls = []
        self.reply = {"status": "accepted"}
        self.ipc_error = None
        self.before_ipc = None
        test = self

        class FakeGateway:
            def __init__(self, **kwargs):
                pass

            def list_requests(self, *, thread_id):
                test.bridge.require_thread_scope(thread_id)
                return {"requests": deepcopy(test.requests)}

            def answer(self, **kwargs):
                test.bridge.require_thread_scope(kwargs["thread_id"])
                if test.before_ipc:
                    test.before_ipc(kwargs)
                test.calls.append(deepcopy(kwargs))
                if test.ipc_error:
                    raise test.ipc_error
                for entry in test.requests:
                    if entry["request"]["key"] == kwargs["request_key"]:
                        entry["state"] = test.reply["status"]
                return deepcopy(test.reply)

        self.client_patch = patch("native_gateway_client.NativeGatewayClient", FakeGateway)
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()
        self.tmp.cleanup()

    def rows(self):
        return decisions._load(self.bridge)

    def publish(self, method=protocol.COMMAND, **extra):
        raw = {"threadId": THREAD, "turnId": "turn-fixture", "itemId": "item-fixture", "startedAtMs": 1, **extra}
        req = protocol.normalize_request(method, raw, "rpc-fixture", "epoch-fixture")
        self.requests.append({"request": req, "state": "pending"})
        gateway.publish_event(self.bridge, "request", {"request": req})
        return req, sorted(self.rows(), key=lambda k: self.rows()[k]["questionIndex"])

    def input(self, *, multiple=False, secret=False):
        questions = [{"id": "first", "header": "Выбор", "question": "  Точный вопрос?\nВторая строка. ", "options": [{"label": "Один", "description": "Первая причина"}, {"label": "Два", "description": "Вторая причина"}], "isSecret": secret}]
        if multiple:
            questions.append({"id": "second", "header": "Ещё", "question": "Второй вопрос?", "options": [{"label": "Три", "description": ""}]})
        return self.publish(protocol.INPUT, questions=questions, isBlocking=True)

    def test_exact_question_options_bot_buttons_and_panel_snapshot(self):
        req, ids = self.input()
        result = decisions.get(self.bridge, ids[0], _recover=False)
        self.assertEqual(result["question"], req["questions"][0]["question"])
        self.assertEqual([o["label"] for o in result["options"]], ["Один", "Два"])
        sent = "".join(part["text"] for part in self.bridge.sent)
        self.assertIn(req["questions"][0]["question"], sent)
        self.assertIn("Один — Первая причина", sent)
        callbacks = [button["callback_data"] for row in self.bridge.sent[-1]["buttons"] for button in row if "callback_data" in button]
        self.assertIn("answer:" + ids[0] + ":0", callbacks)
        self.assertIn("custom:" + ids[0], callbacks)

    def test_choice_delivers_exact_typed_rpc_response_without_queue_fallback(self):
        req, ids = self.input()
        result = decisions.answer(self.bridge, ids[0], choice_id="1")["decision"]
        self.assertEqual(self.calls[0]["response"], {"answers": {"first": {"answers": ["Два"]}}})
        self.assertEqual(self.calls[0]["request_key"], req["key"])
        self.assertEqual(self.calls[0]["thread_id"], THREAD)
        self.assertEqual(result["status"], "accepted_by_server")
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(self.bridge.queued, [])
        self.assertEqual(self.bridge.finished, [])

    def test_custom_answer_text_preserved(self):
        _, ids = self.input()
        answer = " Мой  ответ\nс новой строкой "
        decisions.answer(self.bridge, ids[0], text=answer)
        self.assertEqual(self.calls[0]["response"]["answers"]["first"]["answers"], [answer])

    def test_multi_question_aggregates_once_after_all_answers(self):
        _, ids = self.input(multiple=True)
        result = decisions.answer(self.bridge, ids[0], choice_id="0")["decision"]
        self.assertEqual(result["status"], "waiting_answers")
        self.assertEqual(self.calls, [])
        decisions.answer(self.bridge, ids[1], text="Свой второй")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["response"], {"answers": {"first": {"answers": ["Один"]}, "second": {"answers": ["Свой второй"]}}})
        self.assertTrue(all(row["status"] == "accepted_by_server" for row in self.rows().values()))

    def test_durable_group_and_answer_saved_before_ipc(self):
        req, ids = self.input(multiple=True)
        decisions.answer(self.bridge, ids[0], choice_id="0")
        def inspect_staged(call):
            rows = self.rows()
            self.assertTrue(all(row["status"] == "gateway_submitting" for row in rows.values()))
            self.assertTrue(all(row.get("answer") is not None for row in rows.values()))
            self.assertTrue(all(row["gatewayOperationId"] == call["operation_id"] for row in rows.values()))
            self.assertEqual(call["operation_id"], "decision-" + req["key"])
        self.before_ipc = inspect_staged
        decisions.answer(self.bridge, ids[1], choice_id="0")

    def test_same_answer_is_idempotent_but_different_answer_rejected(self):
        _, ids = self.input()
        decisions.answer(self.bridge, ids[0], choice_id="0")
        decisions.answer(self.bridge, ids[0], choice_id="0")
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(ValueError, "already_answered"):
            decisions.answer(self.bridge, ids[0], choice_id="1")

    def test_owner_scope_checked_before_any_gateway_mutation(self):
        _, ids = self.input()
        with patch.object(self.bridge, "require_thread_scope", side_effect=ValueError("out_of_scope")):
            with self.assertRaisesRegex(ValueError, "out_of_scope"):
                decisions.answer(self.bridge, ids[0], choice_id="0")
        self.assertEqual(self.calls, [])
        self.assertNotIn("answer", self.rows()[ids[0]])

    def test_command_only_explicit_buttons_and_offered_decision(self):
        _, ids = self.publish(availableDecisions=["decline", "accept"], reason="Можно?", command="echo fixture")
        with self.assertRaisesRegex(ValueError, "explicit_button"):
            decisions.answer(self.bridge, ids[0], text="Да")
        result = decisions.answer(self.bridge, ids[0], choice_id="0")["decision"]
        self.assertEqual(self.calls[0]["response"], {"decision": "decline"})
        self.assertFalse(result["customAnswerAllowed"])

    def test_permissions_stay_typed_and_source_scoped(self):
        wanted = {"fileSystem": {"read": ["C:/synthetic"]}, "network": {"enabled": False}}
        _, ids = self.publish(protocol.PERMISSIONS, permissions=wanted)
        decisions.answer(self.bridge, ids[0], choice_id="0")
        self.assertEqual(self.calls[0]["response"], {"permissions": wanted, "scope": "turn"})

    def test_secret_notice_has_no_secret_or_buttons(self):
        _, ids = self.input(secret=True)
        row = self.rows()[ids[0]]
        self.assertEqual(row["status"], "desktop_required")
        self.assertEqual(row["options"], [])
        self.assertFalse(self.bridge.sent[-1]["buttons"])
        with self.assertRaisesRegex(ValueError, "protected_confirmation"):
            decisions.answer(self.bridge, ids[0], text="secret")
        self.assertEqual(self.calls, [])

    def test_unresolved_timeout_has_no_retry_or_queue_fallback(self):
        _, ids = self.input()
        self.ipc_error = GatewayError("native_gateway_timeout", uncertain=True)
        result = decisions.answer(self.bridge, ids[0], choice_id="0")["decision"]
        self.assertEqual(result["status"], "uncertain")
        decisions.answer(self.bridge, ids[0], choice_id="0")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.bridge.queued, [])

    def test_real_resolved_cleanup_closes_unanswered_request(self):
        req, ids = self.input()
        gateway.publish_event(self.bridge, "resolved", {"request": req, "state": "closed"})
        self.requests[0]["state"] = "closed"
        self.assertEqual(decisions.get(self.bridge, ids[0], _recover=False)["status"], "closed")
        result = decisions.answer(self.bridge, ids[0], choice_id="0")["decision"]
        self.assertEqual(result["status"], "closed")
        self.assertIsNone(result["answer"])
        self.assertEqual(self.calls, [])

    def test_live_closed_request_cannot_accept_stale_button(self):
        _, ids = self.input()
        self.requests[0]["state"] = "closed"
        self.ipc_error = GatewayError("request_not_pending", uncertain=False)
        try:
            result = decisions.answer(self.bridge, ids[0], choice_id="0")["decision"]
        except ValueError:
            result = decisions.get(self.bridge, ids[0], _recover=False)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(self.bridge.queued, [])

    def test_crash_before_ipc_does_not_leave_permanent_submitting(self):
        _, ids = self.input()
        rows = self.rows()
        rows[ids[0]].update(status="gateway_submitting", answer={"choiceId": "0", "text": "Один"}, gatewayOperationId="decision-fixture")
        decisions._save(self.bridge, rows)
        result = decisions.get(self.bridge, ids[0], _recover=False)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.calls, [])

    def test_acked_without_saved_owner_answer_is_closed_not_open(self):
        _, ids = self.input()
        self.requests[0]["state"] = "accepted"
        self.assertEqual(decisions.get(self.bridge, ids[0], _recover=False)["status"], "closed")

    def test_custom_flat_form_value_validates_and_delivers(self):
        _, ids = self.publish(protocol.ELICITATION, mode="form", message="Дайте ответ", serverName="fixture", requestedSchema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]})
        decisions.answer(self.bridge, ids[0], text="Свой ответ")
        self.assertEqual(self.calls[0]["response"], {"action": "accept", "content": {"answer": "Свой ответ"}})

    def test_gateway_before_native_sync_keeps_canonical_ids_without_bot_duplicates(self):
        req, canonical = self.input(multiple=True)
        sent_count = len(self.bridge.sent)
        for index, question in enumerate(req["questions"]):
            result = decisions.register(self.bridge, {
                "threadId": THREAD, "itemId": req["itemId"], "title": "Проверочный чат",
                "questionSource": "native_sync", "questionIndex": index,
                "question": question["question"], "options": question.get("options", [])})
            self.assertEqual(result["id"], canonical[index])
            self.assertFalse(result["queued"])
        visible = decisions.list_for_thread(self.bridge, THREAD)["decisions"]
        self.assertEqual({row["id"] for row in visible}, set(canonical))
        self.assertTrue(all(row["questionSource"] == "native_gateway" and row["status"] == "open" for row in visible))
        self.assertEqual(len(self.bridge.sent), sent_count)
        self.assertEqual(len(self.rows()), 2)

    def test_native_sync_before_gateway_supersedes_old_cards_and_stale_buttons(self):
        questions = [
            {"id": "first", "header": "Первый", "question": "Первый вопрос?", "options": [{"label": "Да", "description": ""}]},
            {"id": "second", "header": "Второй", "question": "Второй вопрос?", "options": [{"label": "Нет", "description": ""}]},
        ]
        old_ids = []
        for index, question in enumerate(questions):
            result = decisions.register(self.bridge, {
                "threadId": THREAD, "itemId": "item-fixture", "title": "Проверочный чат",
                "questionSource": "native_sync", "questionIndex": index,
                "question": question["question"], "options": question["options"]})
            old_ids.append(result["id"])
            self.assertEqual(result["status"], "desktop_required")
        req, all_ids = self.publish(protocol.INPUT, questions=questions, isBlocking=True)
        canonical = [key for key in all_ids if self.rows()[key]["questionSource"] == "native_gateway"]
        self.assertEqual(len(canonical), 2)
        visible = decisions.list_for_thread(self.bridge, THREAD)["decisions"]
        self.assertEqual({row["id"] for row in visible}, set(canonical))
        self.assertTrue(all(self.rows()[old]["status"] == "superseded" for old in old_ids))
        for old in old_ids:
            with self.assertRaisesRegex(ValueError, "decision_not_open"):
                decisions.answer(self.bridge, old, choice_id="0")
        sent_count = len(self.bridge.sent)
        gateway.publish_event(self.bridge, "request", {"request": req})
        self.assertEqual(len(self.bridge.sent), sent_count)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

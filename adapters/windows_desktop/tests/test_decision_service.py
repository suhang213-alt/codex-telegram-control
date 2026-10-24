"""Synthetic owner-decision delivery contracts; no network or real chats."""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import decision_service as decisions


THREAD = "00000000-0000-4000-8000-000000000042"


class FakeBridge:
    class OwnerNotPaired(RuntimeError):
        pass

    def __init__(self, root, *, read_delay=0):
        self.APP_DIR = root
        self.DECISION_PROMPTS = root / "decisions.bin"
        self.INBOX = root / "inbox"
        self.INBOX.mkdir(exist_ok=True)
        self.read_delay = read_delay
        self.owner_available = True
        self.fail_before_rename = False
        self.rows = {}
        self.sent = []
        self.queued = []
        self.finished = []
        self.api_calls = []

    def require_thread_scope(self, thread):
        if thread != THREAD:
            raise ValueError("out_of_scope")

    def thread_in_scope(self, thread):
        return thread == THREAD

    def read_json(self, path):
        value = json.loads(path.read_text(encoding="utf-8"))
        if path == self.DECISION_PROMPTS and self.read_delay:
            time.sleep(self.read_delay)
        return value

    def atomic_json(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def reply_payload(self, payload):
        if not self.owner_available:
            raise self.OwnerNotPaired("owner_not_paired")
        self.sent.append(payload)

    def send(self, text, buttons=None):
        self.sent.append({"text": text, "buttons": buttons})

    def state(self):
        return {"owner_chat_id": 42}

    def api(self, method, payload):
        self.api_calls.append((method, payload))
        return {"message_id": 91}

    def prepare_web(self, kind, text, target, settings, attachments):
        self.queued.append((kind, text, target, settings, attachments))
        operation_id = 100 + len(self.queued)
        self.atomic_json(self.INBOX / f"{operation_id}.web_pending", {
            "id": operation_id, "kind": kind, "text": text,
            "selected_thread_id": target})
        return operation_id

    def finish_web(self, operation_id):
        if self.fail_before_rename:
            self.fail_before_rename = False
            raise OSError("synthetic_failure_before_rename")
        staged = self.INBOX / f"{operation_id}.web_pending"
        if staged.exists():
            staged.rename(self.INBOX / f"{operation_id}.bin")
            self.finished.append(operation_id)
        return operation_id


def _decision_process_worker(root, action, decision_id, start, results):
    bridge = FakeBridge(Path(root), read_delay=.2)
    start.wait(10)
    try:
        if action == "answer":
            decisions.answer(bridge, decision_id, choice_id="0")
        else:
            decisions.register(bridge, {"threadId": THREAD, "itemId": "other_process",
                "title": "Чат", "question": "Второй вопрос?", "options": [],
                "questionSource": "explicit_text"})
        results.put(None)
    except Exception as exc:
        results.put(type(exc).__name__ + ":" + str(exc))


def _concurrent_answer_worker(root, decision_id, choice_id, start, results):
    bridge = FakeBridge(Path(root), read_delay=.2)
    start.wait(10)
    try:
        reply = decisions.answer(bridge, decision_id, choice_id=choice_id)
        results.put(("ok", reply["decision"]["operationId"], len(bridge.queued)))
    except Exception as exc:
        results.put(("error", type(exc).__name__ + ":" + str(exc), len(bridge.queued)))


def _die_holding_decision_lock(root):
    bridge = FakeBridge(Path(root))
    with decisions._store_transaction(bridge):
        os._exit(0)


def _hold_decision_lock(root, ready, release):
    bridge = FakeBridge(Path(root))
    with decisions._store_transaction(bridge):
        ready.set()
        release.wait(10)


class DecisionServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = FakeBridge(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, *, question="Что выбрать?", options=None, source="native_async"):
        return decisions.register(self.bridge, {"threadId": THREAD, "itemId": "call_fixture",
            "title": "Проверочный чат", "question": question,
            "options": options if options is not None else [{"label": "Первый", "description": "Причина 1"},
                {"label": "Второй", "description": "Причина 2"}],
            "questionSource": source, "questionIndex": 0})["id"]

    def test_external_native_answer_closes_open_question_and_blocks_second_send(self):
        decision_id = self.register()
        row = self.bridge.read_json(self.bridge.DECISION_PROMPTS)[decision_id]
        path = Path(self.tmp.name) / "external-native.jsonl"
        reply = decisions.build_async_decision_reply(row["itemId"], 0, row["question"], "Мой ответ Desktop")
        event = {"timestamp": row["at"] + 1, "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": THREAD, "turn_id": "turn-external",
            "item": {"type": "UserMessage", "id": "user-external", "client_id": "client-external",
                     "content": [{"type": "text", "text": reply}]}}}
        path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
        with patch("miniapp_server.local_codex_rollout", return_value=path):
            self.assertEqual(decisions.get(self.bridge, decision_id)["status"], "answered_elsewhere")
            self.assertEqual(decisions.list_for_thread(self.bridge, THREAD)["decisions"][0]["status"], "answered_elsewhere")
            with self.assertRaisesRegex(ValueError, "decision_not_open"):
                decisions.answer(self.bridge, decision_id, choice_id="0")
        self.assertEqual(self.bridge.queued, [])
        self.assertIsNone(decisions.get(self.bridge, decision_id)["answer"])

    def test_external_other_question_plain_text_and_missing_native_client_do_not_close(self):
        decision_id = self.register()
        row = self.bridge.read_json(self.bridge.DECISION_PROMPTS)[decision_id]
        path = Path(self.tmp.name) / "external-unrelated.jsonl"
        exact = decisions.build_async_decision_reply(row["itemId"], 0, row["question"], "Да")
        variants = [exact.replace("call_fixture", "call_other"),
                    exact.replace(row["question"], "Другой вопрос?"), "Да", exact]
        with patch("miniapp_server.local_codex_rollout", return_value=path):
            for index, body in enumerate(variants):
                event = {"timestamp": row["at"] + 1, "type": "event_msg", "payload": {
                    "type": "item_completed", "thread_id": THREAD, "turn_id": "turn-external",
                    "item": {"type": "UserMessage", "id": "user-external",
                             "client_id": None if index == 3 else "client-external",
                             "content": [{"type": "text", "text": body}]}}}
                path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
                decisions._scan_native_answer.cache_clear()
                self.assertEqual(decisions.get(self.bridge, decision_id)["status"], "open")

    def test_exact_question_and_options_survive_telegram_chunks_and_panel_read(self):
        question = "Строка 1\n\n" + "Ж" * 7100 + "\nСтрока 2"
        decision_id = self.register(question=question, source="explicit_text")
        chunks = [sent["text"] for sent in self.bridge.sent]
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        joined = "".join(chunks)
        self.assertIn(question, joined)
        self.assertIn("Первый — Причина 1", joined)
        self.assertEqual(decisions.list_for_thread(self.bridge, THREAD)["decisions"][0]["question"], question)
        callbacks = [button["callback_data"] for row in self.bridge.sent[-1]["buttons"]
                     for button in row if "callback_data" in button]
        self.assertIn("answer:" + decision_id + ":0", callbacks)
        self.assertIn("custom:" + decision_id, callbacks)

    def test_emoji_question_is_never_truncated_by_telegram_chunking(self):
        question = "🧭" * 5000 + " финал"
        self.register(question=question, source="explicit_text")
        chunks = [sent["text"] for sent in self.bridge.sent]
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3900 for chunk in chunks))
        self.assertIn(question, "".join(chunks))

    def test_unsent_notification_resumes_without_repeating_confirmed_chunks(self):
        payload = {"threadId": THREAD, "itemId": "resume_fixture", "title": "Чат",
                   "question": "Ж" * 9000, "options": [], "questionSource": "explicit_text"}
        original = self.bridge.reply_payload
        attempts = 0
        def interrupt_before_first_send(value):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic_pre_send_failure")
            original(value)
        self.bridge.reply_payload = interrupt_before_first_send
        with self.assertRaisesRegex(RuntimeError, "synthetic_pre_send_failure"):
            decisions.register(self.bridge, payload)
        row = next(iter(self.bridge.read_json(self.bridge.DECISION_PROMPTS).values()))
        self.assertEqual(row["status"], "notification_uncertain")
        self.assertEqual(row["nextChunk"], 0)
        self.assertEqual(self.bridge.sent, [])

    def test_ready_notification_resumes_after_save_before_network(self):
        payload = {"threadId": THREAD, "itemId": "ready_fixture", "title": "Чат",
                   "question": "Продолжить?", "options": [], "questionSource": "explicit_text"}
        decision_id, record = decisions._validate_payload(payload, self.bridge)
        self.bridge.atomic_json(self.bridge.DECISION_PROMPTS, {decision_id: record})
        self.assertEqual(decisions.register(self.bridge, payload)["status"], "open")
        self.assertEqual(len(self.bridge.sent), 1)
        self.assertFalse(decisions.register(self.bridge, payload)["queued"])
        self.assertEqual(len(self.bridge.sent), 1)

    def test_fast_choice_during_last_notification_is_not_lost(self):
        payload = {"threadId": THREAD, "itemId": "race_fixture", "title": "Чат",
                   "question": "Что выбрать?", "options": ["Да", "Нет"],
                   "questionSource": "native_async"}
        original = self.bridge.reply_payload
        def immediate_owner_choice(value):
            original(value)
            decision_id = value["buttons"][0][0]["callback_data"].split(":")[1]
            decisions.answer(self.bridge, decision_id, choice_id="0")
        self.bridge.reply_payload = immediate_owner_choice
        result = decisions.register(self.bridge, payload)
        self.assertEqual(result["status"], "submitting")
        self.assertEqual(len(self.bridge.queued), 1)

    def test_notification_timeout_cannot_overwrite_concurrent_answer(self):
        payload = {"threadId": THREAD, "itemId": "notify_timeout_fixture", "title": "Чат",
                   "question": "Что выбрать?", "options": ["Да", "Нет"],
                   "questionSource": "native_async"}
        def answer_then_timeout(value):
            decision_id = value["buttons"][0][0]["callback_data"].split(":")[1]
            decisions.answer(self.bridge, decision_id, choice_id="0")
            raise TimeoutError("synthetic_send_uncertain")
        self.bridge.reply_payload = answer_then_timeout
        with self.assertRaisesRegex(TimeoutError, "synthetic_send_uncertain"):
            decisions.register(self.bridge, payload)
        row = next(iter(self.bridge.read_json(self.bridge.DECISION_PROMPTS).values()))
        self.assertEqual(row["status"], "submitting")
        self.assertEqual(row["notificationState"], "uncertain")
        self.assertEqual(row["answer"], {"choiceId": "0", "text": "Да"})
        self.assertFalse(decisions.register(self.bridge, payload)["queued"])

    def test_all_open_decisions_remain_visible_beyond_recent_history_limit(self):
        for index in range(35):
            decisions.register(self.bridge, {"threadId": THREAD, "itemId": f"item_{index}",
                "title": "Чат", "question": f"Вопрос {index}", "options": [],
                "questionSource": "explicit_text"})
        visible = decisions.list_for_thread(self.bridge, THREAD)["decisions"]
        self.assertEqual(len(visible), 35)
        self.assertEqual({row["question"] for row in visible}, {f"Вопрос {i}" for i in range(35)})

    def test_choice_and_free_text_bind_to_native_async_question_once(self):
        decision_id = self.register()
        first = decisions.answer(self.bridge, decision_id, choice_id="1")["decision"]
        self.assertEqual(first["answer"], {"choiceId": "1", "text": "Второй"})
        native = self.bridge.queued[0][1]
        self.assertIn('"questionItemId":"[\\"request_user_input_async\\",\\"call_fixture\\",0]"', native)
        self.assertIn('"answer":"Второй"', native)
        self.assertEqual(self.bridge.queued[0][3], {"decision_answer": True})
        decisions.answer(self.bridge, decision_id, choice_id="1")
        self.assertEqual(len(self.bridge.queued), 1)
        self.assertEqual(self.bridge.finished, [101])
        with self.assertRaisesRegex(ValueError, "decision_already_answered_differently"):
            decisions.answer(self.bridge, decision_id, text="Другой ответ")

    def test_exact_other_client_answer_resolves_missing_queue_without_claiming_delivery(self):
        decision_id = self.register()
        decisions.answer(self.bridge, decision_id, choice_id="1")
        row = self.bridge.read_json(self.bridge.DECISION_PROMPTS)[decision_id]
        path = Path(self.tmp.name) / "synthetic-rollout.jsonl"
        wrapper = decisions.build_async_decision_reply(
            row["itemId"], row["questionIndex"], row["question"], row["answer"]["text"])
        def event(item_id, client_id, body, stamp):
            return {"type": "event_msg", "timestamp": stamp, "payload": {
                "type": "item_completed", "turn_id": "synthetic-turn", "thread_id": THREAD,
                "item": {"type": "UserMessage", "id": item_id, "client_id": client_id,
                         "content": [{"type": "text", "text": body}]}}}
        def write(*records):
            path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n"
                                    for record in records), encoding="utf-8")
            decisions._scan_native_answer.cache_clear()
        earlier = event("earlier", "earlier-client", "Unrelated", row["at"] - 20)
        exact = event("other-user", "other-client", wrapper, row["at"] + 1)
        receipt = {"clientUserMessageId": "bridge-client", "threadId": THREAD}
        missing = lambda _: {"deliveryStage": "uncertain", "deliveryReason": "native_queue_missing"}
        queued = lambda _: {"deliveryStage": "queued", "deliveryReason": "exact_native_queue"}
        with (patch("miniapp_server.local_codex_rollout", return_value=path),
              patch("fast_dispatch.dispatch_receipt", return_value=receipt)):
            write(earlier, exact)
            self.assertEqual(decisions.get(self.bridge, decision_id, missing)["status"],
                             "answered_elsewhere")
            self.assertEqual(decisions.get(self.bridge, decision_id, queued)["status"], "queued")
            write(earlier, {**exact, "payload": {**exact["payload"],
                "item": {**exact["payload"]["item"], "client_id": "bridge-client"}}})
            self.assertEqual(decisions.get(self.bridge, decision_id, missing)["status"], "uncertain")
            write(earlier, exact, event("duplicate-user", "other-client-2", wrapper, row["at"] + 2))
            self.assertEqual(decisions.get(self.bridge, decision_id, missing)["status"], "uncertain")
            write(earlier, event("wrong-answer", "other-client", wrapper.replace("Второй", "Нет"),
                                 row["at"] + 1))
            self.assertEqual(decisions.get(self.bridge, decision_id, missing)["status"], "uncertain")

    def test_native_worker_answer_visible_in_chat_without_bridge_delivery_claim(self):
        decision_id = self.register()
        decisions.answer(self.bridge, decision_id, choice_id="1")
        row = self.bridge.read_json(self.bridge.DECISION_PROMPTS)[decision_id]
        path = Path(self.tmp.name) / "synthetic-worker-rollout.jsonl"
        wrapper = decisions.build_async_decision_reply(
            row["itemId"], row["questionIndex"], row["question"], row["answer"]["text"])
        def event(item_id, body, stamp, thread=THREAD):
            return {"type": "event_msg", "timestamp": stamp, "payload": {
                "type": "item_completed", "turn_id": "synthetic-turn", "thread_id": thread,
                "item": {"type": "UserMessage", "id": item_id, "client_id": item_id,
                         "content": [{"type": "text", "text": body}]}}}
        def write(*records):
            path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n"
                                    for record in records), encoding="utf-8")
            decisions._scan_native_answer.cache_clear()
        earlier = event("earlier", "Unrelated", row["at"] - 20)
        exact = event("native-user", wrapper, row["answerSubmittedAt"] + 1)
        worker = lambda _: {"deliveryStage": "completed", "deliveryReason": "worker_result"}
        with patch("miniapp_server.local_codex_rollout", return_value=path):
            write(earlier, exact)
            self.assertEqual(decisions.get(self.bridge, decision_id, worker)["status"],
                             "answer_seen_in_chat")
            before = decisions._scan_native_answer.cache_info().hits
            self.assertEqual(decisions.get(self.bridge, decision_id, worker)["status"],
                             "answer_seen_in_chat")
            self.assertGreater(decisions._scan_native_answer.cache_info().hits, before)
            write(earlier, event("native-user", wrapper, row["answerSubmittedAt"] - 1))
            self.assertEqual(decisions.get(self.bridge, decision_id, worker)["status"], "uncertain")
            write(earlier, exact, event("duplicate-user", wrapper, row["answerSubmittedAt"] + 2))
            self.assertEqual(decisions.get(self.bridge, decision_id, worker)["status"], "uncertain")
            write(earlier, event("wrong-thread", wrapper, row["answerSubmittedAt"] + 1,
                                 "00000000-0000-4000-8000-000000000099"))
            self.assertEqual(decisions.get(self.bridge, decision_id, worker)["status"], "uncertain")
            write(earlier, event("wrong-answer", wrapper.replace("Второй", "Нет"),
                                 row["answerSubmittedAt"] + 1))
            self.assertEqual(decisions.get(self.bridge, decision_id, worker)["status"], "uncertain")

    def test_custom_telegram_reply_uses_prompt_id_and_preserves_whitespace(self):
        decision_id = self.register(options=[])
        self.assertEqual(decisions.prompt_custom(self.bridge, decision_id), 91)
        self.assertEqual(decisions.custom_reply_target(self.bridge, 91), decision_id)
        self.assertIsNone(decisions.custom_reply_target(self.bridge, 92))
        raw = "  Свой вариант\nс Unicode ✓  "
        decisions.answer(self.bridge, decision_id, text=raw)
        self.assertIn('"answer":"  Свой вариант\\nс Unicode ✓  "', self.bridge.queued[0][1])

    def test_custom_reply_embedded_id_routes_even_without_saved_message_id(self):
        decision_id = self.register(options=[])
        prompt = decisions.CUSTOM_PROMPT_PREFIX + decision_id + "\nСвободный ответ"
        self.assertEqual(decisions.custom_reply_target(self.bridge, 999, prompt), decision_id)
        decisions.answer(self.bridge, decision_id, text="Ответ")
        self.assertEqual(decisions.custom_reply_target(self.bridge, 999, prompt), decision_id)
        self.assertIsNone(decisions.custom_reply_target(self.bridge, 999,
            decisions.CUSTOM_PROMPT_PREFIX + "f" * 20))

    def test_secret_question_is_redacted_in_panel_and_telegram(self):
        decisions.register(self.bridge, {"threadId": THREAD, "itemId": "secret_fixture",
            "title": "Чат", "question": "Введите секрет 12345", "options": ["12345"],
            "questionSource": "native_sync", "questionIndex": 0, "isSecret": True})
        self.assertNotIn("12345", str(self.bridge.sent))
        panel = decisions.list_for_thread(self.bridge, THREAD)["decisions"][0]
        self.assertNotIn("12345", str(panel))
        self.assertEqual(panel["status"], "desktop_required")

    def test_sync_question_has_no_fake_answer_affordance(self):
        decision_id = self.register(source="native_sync")
        self.assertEqual(decisions.get(self.bridge, decision_id)["status"], "desktop_required")
        self.assertFalse(self.bridge.sent[-1]["buttons"])
        with self.assertRaisesRegex(ValueError, "answer_required_in_codex_desktop"):
            decisions.answer(self.bridge, decision_id, choice_id="0")

    def test_oversized_async_question_does_not_offer_broken_buttons(self):
        decision_id = self.register(question="Q" * 12000, options=[])
        self.assertEqual(decisions.get(self.bridge, decision_id)["status"], "desktop_required")
        self.assertFalse(self.bridge.sent[-1]["buttons"])

    def test_register_and_answer_in_separate_processes_preserve_both_changes(self):
        decision_id = self.register()
        context = multiprocessing.get_context("spawn")
        start, results = context.Event(), context.Queue()
        workers = [context.Process(target=_decision_process_worker,
            args=(self.tmp.name, action, decision_id, start, results))
            for action in ("register", "answer")]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual([results.get(timeout=2) for _ in workers], [None, None])
        rows = self.bridge.read_json(self.bridge.DECISION_PROMPTS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[decision_id]["answer"], {"choiceId": "0", "text": "Первый"})
        self.assertIn("other_process", {row["itemId"] for row in rows.values()})

    def test_two_processes_same_choice_reserve_one_native_operation(self):
        decision_id = self.register()
        context = multiprocessing.get_context("spawn")
        start, results = context.Event(), context.Queue()
        workers = [context.Process(target=_concurrent_answer_worker,
            args=(self.tmp.name, decision_id, "0", start, results)) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
            self.assertEqual(worker.exitcode, 0)
        outcome = [results.get(timeout=2) for _ in workers]
        self.assertEqual([row[0] for row in outcome], ["ok", "ok"])
        self.assertEqual({row[1] for row in outcome}, {"101"})
        self.assertEqual(sum(row[2] for row in outcome), 1)
        self.assertEqual(len(list(self.bridge.INBOX.glob("101.bin"))), 1)

    def test_two_processes_conflicting_choices_reject_second_operation(self):
        decision_id = self.register()
        context = multiprocessing.get_context("spawn")
        start, results = context.Event(), context.Queue()
        workers = [context.Process(target=_concurrent_answer_worker,
            args=(self.tmp.name, decision_id, choice, start, results))
            for choice in ("0", "1")]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
            self.assertEqual(worker.exitcode, 0)
        outcome = [results.get(timeout=2) for _ in workers]
        self.assertEqual({row[0] for row in outcome}, {"ok", "error"})
        self.assertIn("decision_already_answered_differently",
                      next(row[1] for row in outcome if row[0] == "error"))
        self.assertEqual(sum(row[2] for row in outcome), 1)

    def test_process_crash_releases_decision_store_lock(self):
        context = multiprocessing.get_context("spawn")
        worker = context.Process(target=_die_holding_decision_lock, args=(self.tmp.name,))
        worker.start()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        with decisions._store_transaction(self.bridge):
            self.assertTrue(self.bridge.DECISION_PROMPTS.with_name("decisions.bin.lock").exists())

    def test_cross_process_lock_contention_is_bounded(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        worker = context.Process(target=_hold_decision_lock,
                                 args=(self.tmp.name, ready, release))
        worker.start()
        try:
            self.assertTrue(ready.wait(10))
            with patch.object(decisions, "_LOCK_TIMEOUT", .12):
                started = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, "decision_store_busy"):
                    decisions.list_for_thread(self.bridge, THREAD)
                self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            worker.join(10)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        self.assertEqual(worker.exitcode, 0)

    def test_failed_publication_recovers_same_operation_on_panel_read(self):
        decision_id = self.register(options=[])
        self.bridge.fail_before_rename = True
        first = decisions.answer(self.bridge, decision_id, text="  Проверенный ответ  ")["decision"]
        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(first["operationId"], "101")
        self.assertTrue((self.bridge.INBOX / "101.web_pending").exists())
        self.assertEqual(self.bridge.finished, [])
        # The next process uses the persisted decision and staged command.
        reopened = FakeBridge(Path(self.tmp.name))
        result = decisions.get(reopened, decision_id)
        self.assertEqual(result["operationId"], "101")
        self.assertFalse((reopened.INBOX / "101.web_pending").exists())
        self.assertTrue((reopened.INBOX / "101.bin").exists())
        self.assertEqual(reopened.finished, [101])
        decisions.get(reopened, decision_id)
        self.assertEqual(reopened.finished, [101])
        self.assertEqual(reopened.read_json(reopened.DECISION_PROMPTS)[decision_id]["publicationState"], "published")

    def test_panel_list_recovers_pending_publication_without_new_operation(self):
        decision_id = self.register(options=[])
        self.bridge.fail_before_rename = True
        decisions.answer(self.bridge, decision_id, text="Ответ через список")
        reopened = FakeBridge(Path(self.tmp.name))
        listed = decisions.list_for_thread(reopened, THREAD)["decisions"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], decision_id)
        self.assertEqual(listed[0]["operationId"], "101")
        self.assertEqual(reopened.finished, [101])
        self.assertEqual(reopened.queued, [])

    def test_recovery_rejects_modified_staged_command(self):
        decision_id = self.register(options=[])
        self.bridge.fail_before_rename = True
        decisions.answer(self.bridge, decision_id, text="Точный ответ")
        path = self.bridge.INBOX / "101.web_pending"
        staged = self.bridge.read_json(path)
        staged["text"] = "Другой ответ"
        self.bridge.atomic_json(path, staged)
        reopened = FakeBridge(Path(self.tmp.name))
        result = decisions.get(reopened, decision_id)
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(path.exists())
        self.assertEqual(reopened.finished, [])

    def test_unpaired_owner_keeps_chunk_ready_without_silent_delivery(self):
        payload = {"threadId": THREAD, "itemId": "unpaired_fixture", "title": "Чат",
                   "question": "Можно продолжить?", "options": [],
                   "questionSource": "explicit_text"}
        self.bridge.owner_available = False
        with self.assertRaises(FakeBridge.OwnerNotPaired):
            decisions.register(self.bridge, payload)
        row = next(iter(self.bridge.read_json(self.bridge.DECISION_PROMPTS).values()))
        self.assertEqual((row["notificationState"], row["nextChunk"]), ("ready", 0))
        self.assertEqual(self.bridge.sent, [])
        self.bridge.owner_available = True
        self.assertTrue(decisions.register(self.bridge, payload)["queued"])
        self.assertEqual(len(self.bridge.sent), 1)


if __name__ == "__main__":
    unittest.main()

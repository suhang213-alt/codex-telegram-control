"""Synthetic native-decision contract checks; no Telegram or Codex mutations."""

from __future__ import annotations

import json
import unittest

from background_native_view import extract_agent_events
from decision_service import build_async_decision_reply


def native_thread(*items: dict) -> dict:
    return {
        "page": {"order": "newest_first"},
        "turns": [{"id": "turn-synthetic", "items": list(items)}],
    }


class AsyncDecisionProtocolTests(unittest.TestCase):
    def test_native_async_question_keeps_all_questions_and_source_text(self):
        questions = [
            {"title": "  Какой вариант?\nВажная строка 🧭  ",
             "options": ["Первый", "Второй — с пояснением"]},
            {"title": "Уточните срок:", "options": None},
        ]
        result = native_thread({
            "type": "agentMessage", "id": "call_synthetic123", "phase": "final_answer",
            "delivery": "async", "text": "Исходный текст сообщения.",
            "questions": questions,
        })
        events = extract_agent_events(result)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["itemId"], "call_synthetic123")
        self.assertEqual(events[0]["turnId"], "turn-synthetic")
        self.assertEqual([item["question"] for item in events[0]["questions"]],
                         [item["title"] for item in questions])
        self.assertEqual([[option["label"] for option in item["options"]]
                          for item in events[0]["questions"]],
                         [questions[0]["options"], []])
        self.assertEqual(events[0]["pendingIndices"], [0, 1])
        self.assertTrue(events[0]["openDecision"])

    def test_unrelated_user_message_does_not_close_native_async_question(self):
        result = native_thread(
            {"type": "agentMessage", "id": "call_synthetic123", "phase": "final_answer",
             "delivery": "async", "text": "Выберите:",
             "questions": [{"title": "Выберите:", "options": ["Да", "Нет"]}]},
            {"type": "userMessage", "id": "user-synthetic", "content": [{"type": "text", "text": "Да"}]},
        )
        events = extract_agent_events(result)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["openDecision"])
        self.assertFalse(events[0]["answered"])
        self.assertEqual(events[0]["pendingIndices"], [0])

    def test_answer_for_one_question_does_not_close_a_second_question(self):
        questions = [{"title": "Первый?", "options": ["Да", "Нет"]},
                     {"title": "Второй?", "options": ["Сейчас", "Позже"]}]
        reply = build_async_decision_reply("call_synthetic123", 0, "Первый?", "Да")
        result = native_thread(
            {"type": "agentMessage", "id": "call_synthetic123", "phase": "final_answer",
             "delivery": "async", "text": "", "questions": questions},
            {"type": "userMessage", "id": "user-synthetic",
             "content": [{"type": "text", "text": reply}]},
        )
        events = extract_agent_events(result)
        self.assertEqual(events[0]["pendingIndices"], [1])
        self.assertTrue(events[0]["openDecision"])
        self.assertFalse(events[0]["answered"])

    def test_answer_for_other_call_does_not_close_this_request(self):
        reply = build_async_decision_reply("call_other", 0, "Другой?", "Да")
        result = native_thread(
            {"type": "agentMessage", "id": "call_synthetic123", "phase": "final_answer",
             "delivery": "async", "text": "",
             "questions": [{"title": "Первый?", "options": ["Да", "Нет"]}]},
            {"type": "userMessage", "id": "user-synthetic",
             "content": [{"type": "text", "text": reply}]},
        )
        events = extract_agent_events(result)
        self.assertEqual(events[0]["pendingIndices"], [0])
        self.assertTrue(events[0]["openDecision"])

    def test_selected_option_matches_native_user_reply_wire_format(self):
        result = build_async_decision_reply(
            "call_synthetic123", 0, "Продолжать?", "Да — рекомендовано"
        )
        expected = (
            '<send_user_message_question_reply>\n'
            '[{"questionItemId":"[\\"request_user_input_async\\",\\"call_synthetic123\\",0]",'
            '"question":"Продолжать?","answer":"Да — рекомендовано"}]\n'
            '</send_user_message_question_reply>\n'
        )
        self.assertEqual(result, expected)

    def test_free_text_preserves_whitespace_unicode_and_question_index(self):
        question = "  Выберите путь?\nБез сокращений  "
        answer = "  Мой вариант:\n• этап 1\n• этап 2 🚀  "
        result = build_async_decision_reply("call_synthetic123", 1, question, answer)
        self.assertTrue(result.startswith("<send_user_message_question_reply>\n"))
        self.assertTrue(result.endswith("\n</send_user_message_question_reply>\n"))
        body = result.split("\n", 1)[1].rsplit("\n</send_user_message_question_reply>\n", 1)[0]
        payload = json.loads(body)
        self.assertEqual(payload, [{
            "questionItemId": '["request_user_input_async","call_synthetic123",1]',
            "question": question, "answer": answer,
        }])

    def test_long_original_question_is_not_truncated_or_reworded(self):
        question = "Решение?\n" + ("Текст 1:1 🧭\n" * 500)
        result = build_async_decision_reply("call_synthetic123", 2, question, "Свой ответ")
        body = result.split("\n", 1)[1].rsplit("\n</send_user_message_question_reply>\n", 1)[0]
        self.assertEqual(json.loads(body)[0]["question"], question)


if __name__ == "__main__":
    unittest.main()

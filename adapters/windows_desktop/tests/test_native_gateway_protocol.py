"""Regression tests for live RPC identity, owner decisions and secret isolation."""
from copy import deepcopy
import json
import unittest

import native_gateway_protocol as p


def params(**extra):
    return {"threadId": "thread-a", "turnId": "turn-a", "itemId": "item-a",
            "startedAtMs": 1, **extra}


def request(method=p.COMMAND, **extra):
    return p.normalize_request(method, params(**extra), 12, "epoch-a")


class IdentityTests(unittest.TestCase):
    def test_integer_and_string_ids_are_distinct(self):
        self.assertNotEqual(p.typed_key(12), p.typed_key("12"))
        self.assertNotEqual(request()["key"], p.normalize_request(p.COMMAND, params(), "12", "epoch-a")["key"])

    def test_bool_float_missing_or_huge_id_rejected(self):
        for value in (True, 1.0, None, "", 2**63):
            with self.subTest(value=value), self.assertRaises(p.ProtocolError):
                p.typed_key(value)

    def test_connection_epoch_changes_identity(self):
        self.assertNotEqual(request()["key"], p.normalize_request(p.COMMAND, params(), 12, "epoch-b")["key"])

    def test_callback_of_same_parent_has_separate_identity(self):
        self.assertNotEqual(request(approvalId="a")["key"], request(approvalId="b")["key"])

    def test_changed_options_or_reason_invalidate_stale_key(self):
        first = request(reason="Точно\n1 в 1", availableDecisions=["decline", "accept"])
        second = request(reason="Точно\n1 в 1", availableDecisions=["accept", "decline"])
        self.assertNotEqual(first["key"], second["key"])
        first["options"].reverse()
        with self.assertRaisesRegex(p.ProtocolError, "stale_or_modified"):
            p.build_response(first, choice=0)

    def test_request_is_defensively_copied(self):
        raw = params(availableDecisions=["decline", "accept"])
        normalized = p.normalize_request(p.COMMAND, raw, 1, "e")
        raw["availableDecisions"].reverse()
        self.assertEqual(p.build_response(normalized, choice=0), {"decision": "decline"})

    def test_missing_thread_turn_item_is_rejected(self):
        for name in ("threadId", "turnId", "itemId"):
            source = params(); del source[name]
            with self.subTest(name=name), self.assertRaises(p.ProtocolError):
                p.normalize_request(p.COMMAND, source, 1, "e")

    def test_nonhuman_transport_requests_are_passthrough(self):
        for method in ("item/tool/call", "attestation/generate", "account/chatgptAuthTokens/refresh", "currentTime/read", "unknown"):
            with self.subTest(method=method):
                self.assertIsNone(p.normalize_request(method, {"token": "never project"}, 1, "e"))


class ApprovalTests(unittest.TestCase):
    def test_no_implicit_approval(self):
        with self.assertRaisesRegex(p.ProtocolError, "explicit_decision"):
            p.build_response(request())

    def test_preserves_exact_reason_command_and_order(self):
        r = request(reason="  Разрешить?\nСохранить  пробелы. ", command='echo "a"', availableDecisions=["decline", "accept"])
        self.assertEqual(r["question"], "  Разрешить?\nСохранить  пробелы. ")
        self.assertEqual(r["command"], 'echo "a"')
        self.assertEqual([x["value"] for x in r["options"]], ["decline", "accept"])

    def test_command_cannot_invent_decision_not_offered(self):
        r = request(availableDecisions=["decline"])
        with self.assertRaisesRegex(p.ProtocolError, "not_offered"):
            p.validate_response(r, {"decision": "accept"})

    def test_typed_network_policy_decision_preserved(self):
        choice = {"applyNetworkPolicyAmendment": {"network_policy_amendment": {"host": "example.test", "action": "allow"}}}
        r = request(availableDecisions=[choice, "decline"])
        self.assertEqual(p.build_response(r, choice=0), {"decision": choice})
        changed = deepcopy(choice); changed["applyNetworkPolicyAmendment"]["network_policy_amendment"]["host"] = "other.test"
        with self.assertRaises(p.ProtocolError):
            p.validate_response(r, {"decision": changed})

    def test_invalid_offered_decisions_fail_closed(self):
        for value in ([], ["approve"], [{"applyNetworkPolicyAmendment": None}]):
            with self.subTest(value=value), self.assertRaises(p.ProtocolError):
                request(availableDecisions=value)

    def test_free_text_cannot_become_system_permission(self):
        with self.assertRaises(p.ProtocolError):
            p.build_response(request(), choice="accept", text="anything")

    def test_file_change_uses_v2_values(self):
        r = request(p.FILE, grantRoot="C:/fixture")
        self.assertEqual(p.build_response(r, choice="decline"), {"decision": "decline"})
        with self.assertRaises(p.ProtocolError):
            p.validate_response(r, {"decision": "approved"})

    def test_legacy_approval_preserves_legacy_enumeration(self):
        for method in (p.LEGACY_COMMAND, p.LEGACY_FILE):
            r = p.normalize_request(method, {"conversationId": "a", "callId": "b", "command": ["echo", "fixture"]}, "rpc-a", "e")
            self.assertEqual(p.build_response(r, choice="approved"), {"decision": "approved"})
            self.assertEqual(p.build_response(r, choice=2, text="Не подходит"), {"decision": {"denied": {"rejection": "Не подходит"}}})
            with self.assertRaises(p.ProtocolError):
                p.validate_response(r, {"decision": "accept"})


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.r = request(p.PERMISSIONS, permissions={"fileSystem": {"read": ["C:/a", "C:/b"], "write": ["C:/a"], "globScanMaxDepth": 5}, "network": {"enabled": True}})

    def test_accept_and_decline_build_typed_grants(self):
        self.assertEqual(p.build_response(self.r, choice=2), {"permissions": {}, "scope": "turn"})
        self.assertEqual(p.build_response(self.r, choice=1)["scope"], "session")

    def test_partial_subset_and_network_denial_allowed(self):
        self.assertTrue(p.validate_response(self.r, {"permissions": {"fileSystem": {"read": ["C:/b"], "globScanMaxDepth": 2}, "network": {"enabled": False}}}))

    def test_new_file_right_or_increased_scan_depth_is_rejected(self):
        for grant in ({"fileSystem": {"write": ["C:/b"]}}, {"fileSystem": {"read": ["C:/outside"]}}, {"fileSystem": {"globScanMaxDepth": 6}}, {"fileSystem": {"unknown": True}}):
            with self.subTest(grant=grant), self.assertRaises(p.ProtocolError):
                p.validate_response(self.r, {"permissions": grant})

    def test_network_cannot_be_enabled_when_not_requested(self):
        r = request(p.PERMISSIONS, permissions={"fileSystem": {"read": ["C:/a"]}})
        with self.assertRaises(p.ProtocolError):
            p.validate_response(r, {"permissions": {"network": {"enabled": True}}})

    def test_permission_types_and_extra_envelope_fields_rejected(self):
        for response in ({"permissions": {}, "scope": "always"}, {"permissions": {}, "strictAutoReview": 1}, {"permissions": {}, "dangerously": True}):
            with self.subTest(response=response), self.assertRaises(p.ProtocolError):
                p.validate_response(self.r, response)

    def test_entry_grants_cannot_remove_requested_denials(self):
        positive = {"access": "write", "path": {"type": "path", "path": "C:/fixture"}}
        denial = {"access": "deny", "path": {"type": "path", "path": "C:/fixture/private"}}
        r = request(p.PERMISSIONS, permissions={"fileSystem": {"entries": [positive, denial]}})
        self.assertTrue(p.validate_response(r, {"permissions": {"fileSystem": {"entries": [positive, denial]}}}))
        with self.assertRaises(p.ProtocolError):
            p.validate_response(r, {"permissions": {"fileSystem": {"entries": [positive]}}})

    def test_invalid_access_and_path_shapes_fail_closed(self):
        for entry in ({"access": "execute", "path": {"type": "path", "path": "C:/a"}}, {"access": "write", "path": "C:/a"}, {"access": "write", "path": {"type": "special", "value": {"kind": "invented"}}}):
            with self.subTest(entry=entry), self.assertRaises(p.ProtocolError):
                request(p.PERMISSIONS, permissions={"fileSystem": {"entries": [entry]}})


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self.q = {"id": "which", "header": "Выбор", "question": "Какой вариант?\nСохранить текст.", "options": [{"label": "А", "description": "Первый"}, {"label": "Б", "description": "Второй"}]}

    def test_exact_options_and_custom_text(self):
        r = request(p.INPUT, questions=[self.q], isBlocking=True)
        self.assertEqual(r["question"], self.q["question"])
        self.assertEqual(p.build_response(r, choice=1), {"answers": {"which": {"answers": ["Б"]}}})
        self.assertEqual(p.build_response(r, text="Мой  вариант\n1"), {"answers": {"which": {"answers": ["Мой  вариант\n1"]}}})

    def test_multi_question_requires_each_exact_id(self):
        second = {**self.q, "id": "next"}
        r = request(p.INPUT, questions=[self.q, second], isBlocking=True)
        self.assertTrue(p.validate_response(r, p.build_response(r, answers={"which": "А", "next": "Свой"})))
        for response in ({"answers": {"which": {"answers": ["А"]}}}, {"answers": {"which": {"answers": ["А"]}, "other": {"answers": ["Б"]}}}):
            with self.assertRaises(p.ProtocolError):
                p.validate_response(r, response)

    def test_empty_or_nontext_answer_cannot_close_question(self):
        r = request(p.INPUT, questions=[self.q])
        for text in (None, " ", True):
            with self.subTest(text=text), self.assertRaises(p.ProtocolError):
                p.build_response(r, text=text)

    def test_secret_question_is_notice_without_values(self):
        secret = {**self.q, "isSecret": True, "options": [{"label": "SECRET-FIXTURE", "description": "SECRET-DESCRIPTION"}]}
        r = request(p.INPUT, questions=[secret])
        self.assertTrue(r["desktopRequired"])
        self.assertNotIn("SECRET", json.dumps(r))
        with self.assertRaisesRegex(p.ProtocolError, "desktop_required"):
            p.build_response(r, text="secret")


class ElicitationTests(unittest.TestCase):
    def make(self, schema, mode="form", **extra):
        return request(p.ELICITATION, serverName="fixture", mode=mode, requestedSchema=schema, message="Точный вопрос?", **extra)

    def test_single_select_enum_preserves_labels_and_values(self):
        schema = {"type": "object", "properties": {"decision": {"type": "string", "enum": ["approved", "rejected"], "enumNames": ["Да", "Нет"]}}, "required": ["decision"]}
        r = self.make(schema)
        self.assertEqual(r["options"][0]["label"], "Да")
        self.assertEqual(p.build_response(r, choice=0), {"action": "accept", "content": {"decision": "approved"}})

    def test_structured_form_validates_numeric_and_boolean_types(self):
        schema = {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 3}, "confirmed": {"type": "boolean"}}, "required": ["count", "confirmed"]}
        r = self.make(schema)
        self.assertEqual(p.build_response(r, choice=0, answers={"count": 2, "confirmed": True})["content"]["count"], 2)
        for content in ({"count": True, "confirmed": True}, {"count": 4, "confirmed": True}, {"count": 2, "confirmed": "true"}, {"count": 2}, {"count": 2, "confirmed": True, "extra": 1}):
            with self.subTest(content=content), self.assertRaises(p.ProtocolError):
                p.validate_response(r, {"action": "accept", "content": content})

    def test_free_text_single_string_and_decline(self):
        r = self.make({"type": "object", "properties": {"answer": {"type": "string", "minLength": 1}}})
        self.assertEqual(p.build_response(r, choice=0, text="Свой ответ")["content"], {"answer": "Свой ответ"})
        self.assertEqual(p.build_response(r, choice=1), {"action": "decline", "content": None})

    def test_unsupported_or_protected_schema_requires_desktop(self):
        for schema in ({"type": "object", "properties": {"password": {"type": "string", "default": "SECRET-FIXTURE"}}}, {"type": "object", "properties": {"nested": {"type": "object"}}}, {"type": "object", "properties": {"email": {"type": "string", "format": "email"}}}):
            r = self.make(schema)
            self.assertTrue(r["desktopRequired"])
            self.assertNotIn("SECRET-FIXTURE", json.dumps(r))

    def test_url_and_userverification_never_expose_proofs(self):
        for mode in ("url", "openai/userVerification"):
            r = self.make(None, mode, url="https://example.test/?token=SECRET-FIXTURE", challenge="SECRET-CHALLENGE")
            self.assertTrue(r["desktopRequired"])
            self.assertNotIn("SECRET", json.dumps(r))
            with self.assertRaises(p.ProtocolError):
                p.validate_response(r, {"action": "accept"})


class ResolvedTests(unittest.TestCase):
    def test_real_ack_distinguishes_answer_from_lifecycle_cleanup(self):
        r = request()
        ack = {"threadId": "thread-a", "requestId": 12}
        self.assertEqual(p.resolved_event(r, ack, submitted=True)["state"], "accepted_by_server")
        self.assertEqual(p.resolved_event(r, ack)["state"], "closed_without_local_answer")

    def test_cross_thread_and_typed_id_ack_rejected(self):
        for ack in ({"threadId": "other", "requestId": 12}, {"threadId": "thread-a", "requestId": "12"}):
            with self.assertRaises(p.ProtocolError):
                p.resolved_event(request(), ack, submitted=True)


if __name__ == "__main__":
    unittest.main()

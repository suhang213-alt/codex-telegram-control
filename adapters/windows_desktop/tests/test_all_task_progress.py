"""Synthetic bounded native progress contracts. No auth, service or real rollout."""
import datetime as dt
import importlib
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    module = importlib.import_module("all_task_progress")
except ModuleNotFoundError as exc:
    if exc.name != "all_task_progress":
        raise
    module = None

ID = "1b566eb1-3d5b-5000-96b7-7be2779da1c3"
OTHER = "d09895d2-7b8e-57fc-af4a-374057b14f05"
THIRD = "30c71c01-f6f1-599c-99a4-3a1a00677dbf"
NOW = dt.datetime.fromisoformat("2026-10-20T21:30:00+03:00")
STAMP = "2026-10-20T21:29:00+03:00"


def record(kind, payload, at=STAMP):
    return {"timestamp": at, "type": kind, "payload": payload}


def started(turn="turn-1"):
    return record("event_msg", {"type": "task_started", "turn_id": turn, "started_at": STAMP})


def message(text, role="assistant", phase="commentary"):
    return record("response_item", {"type": "message", "role": role, "phase": phase,
                                    "content": [{"type": "output_text", "text": text}]})


def table(done=1):
    states = ["Готово" if i < done else "В работе" if i == done else "Далее" for i in range(3)]
    return ("| Задача и результат | Статус | Оценка длительности | Ожидаемая готовность | Осталось |\n"
            "|---|---|---|---|---|\n" + "\n".join("| Этап " + str(i) + " | " + state + " | 1 мин | Скоро | 1 мин |" for i, state in enumerate(states)))


class AllTaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, "Generic bounded progress reader is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / ".codex"
        self.sessions = self.codex / "sessions"
        self.sessions.mkdir(parents=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.rows = []
        self.path = self.add_source(ID, [started(), message(table())])

    def add_source(self, identifier, records, **changes):
        path = self.sessions / ("rollout-" + identifier + ".jsonl")
        header = record("session_meta", {"id": identifier, "source": "vscode"})
        path.write_bytes(b"".join((json.dumps(v, ensure_ascii=False) + "\n").encode("utf-8") for v in [header] + records))
        row = {"id": identifier, "rollout_path": str(path), "cwd": str(self.workspace), "title": "Проверка задачи",
               "thread_source": "user", "source": "vscode", "archived": 0, "agent_path": None,
               "agent_role": None, "updated_at_ms": int(NOW.timestamp() * 1000)}
        row.update(changes)
        self.rows.append(row)
        return path

    def service(self, **options):
        return module.AllTaskProgress(metadata_provider=lambda: self.rows, codex_root=self.codex, **options)

    def snapshot(self, service=None, identifier=ID, **kwargs):
        return (service or self.service()).snapshot(identifier, now=NOW, **kwargs)

    def append(self, path, *values):
        with path.open("ab") as handle:
            for value in values:
                handle.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))

    def protocol(self, done=2, **changes):
        data = {"schema": "codex-task-status/1", "task_id": ID, "task": "Явный статус",
                "overall_status": "В работе", "current": "Сохранённый этап", "forecast": "",
                "started_at": STAMP, "updated_at": STAMP,
                "checkpoints": [{"id": str(i), "label": "Этап " + str(i), "done": i < done} for i in range(3)],
                "lanes": [], "metrics": {}}
        data.update(changes)
        return data

    def test_strict_public_table_yields_real_counts_and_active_runtime(self):
        result = self.snapshot()
        self.assertTrue(result["available"])
        self.assertEqual((result["done"], result["total"], result["percent"]), (1, 3, 33))
        self.assertEqual(result["runtime_status"], "active")
        self.assertTrue(result["turn_confirmed"])
        self.assertEqual(result["progress_basis"], "assistant_table")

    def test_new_turn_discards_previous_completed_hundred_percent(self):
        self.append(self.path, message(table(3)), record("event_msg", {"type": "task_complete", "turn_id": "turn-1"}),
                    started("turn-2"))
        result = self.snapshot()
        self.assertEqual(result["runtime_status"], "active")
        self.assertIsNone(result["percent"])
        self.assertFalse(result["final"])

    def test_missing_boundary_in_cold_tail_never_reuses_a_hundred_percent_phrase(self):
        self.append(self.path, message("x" * 3000), message("Прогресс: выполнено 3 из 3 задач."))
        result = self.snapshot(self.service(initial_bytes=700, line_bytes=512))
        self.assertFalse(result["turn_confirmed"])
        self.assertIsNone(result["percent"])

    def test_exact_workspace_protocol_survives_cold_tail_without_claiming_native_turn(self):
        folder = self.workspace / ".task-progress"
        folder.mkdir()
        snapshot_path = folder / (ID + ".json")
        snapshot_path.write_text(json.dumps(self.protocol()), encoding="utf-8")
        self.append(self.path, message("x" * 3000), message("Unrelated assistant text"))
        service = self.service(initial_bytes=700, line_bytes=512)
        result = self.snapshot(service)
        self.assertEqual((result["thread_id"], result["percent"]), (ID, 67))
        self.assertEqual(result["progress_basis"], "workspace_protocol_unconfirmed_turn")
        self.assertFalse(result["turn_confirmed"])
        self.assertTrue(result["stale"])
        self.assertFalse(result["running"])

        snapshot_path.write_text(json.dumps(self.protocol(task_id=OTHER)), encoding="utf-8")
        self.assertIsNone(self.snapshot(service)["percent"], "Another chat's snapshot must not be displayed")
        snapshot_path.write_text(json.dumps(self.protocol()), encoding="utf-8")
        self.append(self.path, record("response_item", {"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text":
                                  '<send_user_message_question_reply>\n[]\n</send_user_message_question_reply>'}]},
                              at=NOW.isoformat()))
        self.assertEqual(self.snapshot(service)["percent"], 67,
                         "Answering an existing question does not start a new task phase")
        self.append(self.path, record("response_item", {"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "New request"}]},
                    at=(NOW - dt.timedelta(seconds=1)).isoformat()))
        self.assertIsNone(self.snapshot(service)["percent"], "A newer user request invalidates the saved phase")

    def test_exact_workspace_protocol_survives_incremental_rollout_backlog(self):
        folder = self.workspace / ".task-progress"
        folder.mkdir()
        snapshot_path = folder / (ID + ".json")
        snapshot_path.write_text(json.dumps(self.protocol()), encoding="utf-8")
        service = self.service(append_bytes=512)
        self.assertEqual(self.snapshot(service)["percent"], 67)
        self.append(self.path, record("response_item", {"type": "function_call_output", "output": "x" * 3000}))
        behind = self.snapshot(service)
        self.assertEqual((behind["thread_id"], behind["percent"]), (ID, 67))
        self.assertEqual(behind["progress_basis"], "workspace_protocol_unconfirmed_turn")
        self.assertTrue(behind["stale"])
        self.assertFalse(behind["turn_confirmed"])
        for _ in range(10):
            caught = self.snapshot(service)
        self.assertEqual(caught["progress_basis"], "workspace_protocol")

        self.append(self.path, record("response_item", {"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "New request"}]}, at=NOW.isoformat()))
        invalidated = self.snapshot(service)
        self.assertIsNone(invalidated["percent"], "New user request must invalidate the old snapshot")

    def test_new_user_behind_unparsed_backlog_hides_previous_phase_immediately(self):
        folder = self.workspace / ".task-progress"
        folder.mkdir()
        (folder / (ID + ".json")).write_text(json.dumps(self.protocol()), encoding="utf-8")
        service = self.service(append_bytes=512)
        self.assertEqual(self.snapshot(service)["percent"], 67)
        self.append(self.path,
                    record("response_item", {"type": "function_call_output", "output": "x" * 10000},
                           at=NOW.isoformat()),
                    record("response_item", {"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": "New request"}]}, at=NOW.isoformat()))
        result = self.snapshot(service)
        self.assertIsNone(result["percent"], "An unread new request must hide the previous phase")
        self.assertFalse(result["turn_confirmed"])
        self.assertEqual(result["current"], "")
        self.assertIsNone(result["started_at_msk"])

    def test_new_user_before_native_turn_start_does_not_preserve_old_phase(self):
        folder = self.workspace / ".task-progress"
        folder.mkdir()
        snapshot_path = folder / (ID + ".json")
        snapshot_path.write_text(json.dumps(self.protocol()), encoding="utf-8")
        service = self.service()
        self.assertEqual(self.snapshot(service)["percent"], 67)
        self.append(self.path, record("response_item", {"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "New request"}]},
                    at=(NOW - dt.timedelta(seconds=1)).isoformat()))
        pending = self.snapshot(service)
        self.assertIsNone(pending["percent"])
        self.assertEqual(pending["current"], "")
        self.assertIsNone(pending["started_at_msk"])
        self.assertFalse(pending["turn_confirmed"])
        snapshot_path.write_text(json.dumps(self.protocol(done=1, updated_at=NOW.isoformat())), encoding="utf-8")
        local = self.snapshot(service)
        self.assertEqual(local["percent"], 33)
        self.assertEqual(local["progress_basis"], "workspace_protocol_unconfirmed_turn")
        self.assertFalse(local["turn_confirmed"])
        self.append(self.path, record("event_msg", {"type": "task_started", "turn_id": "turn-2",
                                      "started_at": NOW.isoformat()}, at=NOW.isoformat()))
        confirmed = self.snapshot(service)
        self.assertEqual(confirmed["percent"], 33)
        self.assertTrue(confirmed["turn_confirmed"])

    def test_technical_question_reply_behind_backlog_keeps_same_phase(self):
        folder = self.workspace / ".task-progress"
        folder.mkdir()
        (folder / (ID + ".json")).write_text(json.dumps(self.protocol()), encoding="utf-8")
        service = self.service(append_bytes=512)
        self.assertEqual(self.snapshot(service)["percent"], 67)
        self.append(self.path,
                    record("response_item", {"type": "function_call_output", "output": "x" * 10000},
                           at=NOW.isoformat()),
                    record("response_item", {"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text":
                               '<send_user_message_question_reply>\n[]\n</send_user_message_question_reply>'}]},
                           at=NOW.isoformat()))
        result = self.snapshot(service)
        self.assertEqual(result["percent"], 67)
        self.assertFalse(result["turn_confirmed"])

    def test_exact_progress_phrase_supplies_denominator_without_inventing_steps(self):
        self.append(self.path, message("Прогресс: выполнено 2 из 5 задач."))
        result = self.snapshot()
        self.assertEqual((result["done"], result["total"], result["percent"]), (2, 5, 40))
        self.assertEqual(result["progress_basis"], "assistant_phrase")
        self.assertEqual(result["checkpoints"], [])

    def test_structured_update_plan_supports_half_rounding_and_ignores_tool_output(self):
        plan = [{"step": "Этап " + str(i), "status": "completed" if i == 0 else "pending"} for i in range(8)]
        self.append(self.path, record("response_item", {"type": "function_call", "name": "functions.update_plan",
                    "arguments": json.dumps({"plan": plan})}),
                    record("response_item", {"type": "function_call_output", "output": "Прогресс: выполнено 8 из 8 задач."}))
        result = self.snapshot()
        self.assertEqual((result["percent"], result["progress_basis"]), (13, "update_plan"))

    def test_user_analysis_and_tool_text_never_override_public_progress(self):
        for role, phase in [("user", "commentary"), ("assistant", "analysis")]:
            self.append(self.path, message("Прогресс: выполнено 3 из 3 задач.", role, phase))
        self.assertIsNone(self.snapshot()["percent"], "A new owner input supersedes the previous phase")

    def test_completion_preserves_partial_plan_and_interruption_never_yields_hundred(self):
        self.append(self.path, record("event_msg", {"type": "task_complete", "turn_id": "turn-1"}))
        result = self.snapshot()
        self.assertEqual((result["runtime_status"], result["percent"]), ("completed", 33))
        self.append(self.path, started("turn-2"), message(table(3)),
                    record("event_msg", {"type": "turn_aborted", "turn_id": "turn-2"}))
        result = self.snapshot()
        self.assertEqual(result["runtime_status"], "interrupted")
        self.assertIsNone(result["percent"])

    def test_workspace_protocol_is_primary_only_when_identity_and_turn_freshness_match(self):
        folder = self.workspace / ".task-progress"
        folder.mkdir()
        path = folder / (ID + ".json")
        path.write_text(json.dumps(self.protocol()), encoding="utf-8")
        self.assertEqual(self.snapshot()["percent"], 67)
        self.assertEqual(self.snapshot()["progress_basis"], "workspace_protocol")
        path.write_text(json.dumps(self.protocol(task_id=OTHER)), encoding="utf-8")
        self.assertEqual(self.snapshot()["percent"], 33)
        path.write_text(json.dumps(self.protocol(updated_at="2026-10-20T20:00:00+03:00")), encoding="utf-8")
        self.assertEqual(self.snapshot()["percent"], 33)

    def test_stale_legacy_final_cannot_cover_new_turn(self):
        old = self.protocol(3, started_at="2026-10-20T20:00:00+03:00", updated_at="2026-10-20T20:01:00+03:00",
                            finished_at="2026-10-20T20:01:00+03:00", overall_status="Завершено")
        self.assertEqual(self.snapshot(self.service(legacy_provider=lambda identifier: old))["percent"], 33)

    def test_private_metadata_rejected_before_open_and_agent_created_needs_public_proof(self):
        self.rows[0]["agent_path"] = "/root/private_child"
        with patch.object(Path, "open", side_effect=AssertionError("must not read private content")):
            self.assertFalse(self.snapshot()["available"])
        self.rows[0]["agent_path"] = None
        self.rows[0]["thread_source"] = "agent_created_thread"
        self.assertFalse(self.snapshot()["available"])
        trusted = {"id": ID, "kind": "codex", "hostId": "local", "status": "active", "native_verification": "codex_native_tools"}
        self.assertTrue(self.snapshot(row=trusted)["available"])

    def test_header_identity_and_subagent_rejected_even_with_public_database_row(self):
        for header in [{"id": OTHER, "source": "vscode"}, {"id": ID, "source": {"subagent": {"parent_thread_id": OTHER}}}]:
            self.path.write_text(json.dumps(record("session_meta", header)) + "\n", encoding="utf-8")
            self.assertFalse(self.snapshot()["available"])

    def test_missing_metadata_and_unc_or_device_paths_fail_closed_without_content_read(self):
        self.assertFalse(self.snapshot(identifier=OTHER)["available"])
        for bad in ["\\\\server\\share\\thread.jsonl", "\\\\?\\C:\\private\\thread.jsonl"]:
            self.rows[0]["rollout_path"] = bad
            with patch.object(Path, "open", side_effect=AssertionError("must not follow untrusted path")):
                self.assertFalse(self.snapshot()["available"])

    def test_incremental_append_partial_lines_and_rotation_reset_old_plan(self):
        service = self.service()
        self.assertEqual(self.snapshot(service)["percent"], 33)
        before = service.coverage()["bytes_read"]
        value = (json.dumps(message("Прогресс: выполнено 2 из 3 задач."), ensure_ascii=False)).encode("utf-8")
        with self.path.open("ab") as handle:
            handle.write(value)
        partial = self.snapshot(service)
        self.assertIsNone(partial["percent"])
        self.assertFalse(partial["turn_confirmed"])
        with self.path.open("ab") as handle:
            handle.write(b"\n")
        self.assertEqual(self.snapshot(service)["percent"], 67)
        # At most a fixed 64-byte identity anchor per append, never the prior history.
        self.assertLessEqual(service.coverage()["bytes_read"] - before, len(value) + 1 + 128)
        self.path.write_text(json.dumps(record("session_meta", {"id": ID, "source": "vscode"})) + "\n", encoding="utf-8")
        self.assertIsNone(self.snapshot(service)["percent"])

    def test_append_hard_budget_lru_and_discovery_limits_are_explicit(self):
        self.add_source(OTHER, [started(), message(table())])
        self.add_source(THIRD, [started(), message(table())])
        service = self.service(cache_size=2, discovery_limit=2, append_bytes=512)
        self.assertEqual(len(service.discover([])), 2)
        self.assertTrue(service.discovery_truncated)
        for identifier in [ID, OTHER, THIRD]:
            self.snapshot(service, identifier)
        self.assertEqual(service.coverage()["cache_entries"], 2)
        before = service.coverage()["bytes_read"]
        self.append(self.sessions / ("rollout-" + THIRD + ".jsonl"), message("x" * 3000))
        self.snapshot(service, THIRD)
        self.assertLessEqual(service.coverage()["bytes_read"] - before, 512)

    def test_reported_checks_are_labeled_and_do_not_expose_paths_commands_or_keys(self):
        self.append(self.path, message("Проверки: 3/3 PASS; файл C:\\owner\\private.txt; api_key=private-value"))
        result = self.snapshot()
        self.assertTrue(result["results"])
        self.assertTrue(all(r["provenance"] == "agent_report" for r in result["results"]))
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("C:\\owner", json.dumps(result))

    def test_quoted_or_fenced_progress_examples_cannot_claim_completion(self):
        self.append(self.path, message('> Прогресс: выполнено 3 из 3 задач.\n```text\nПрогресс: выполнено 3 из 3 задач.\n```'))
        self.assertEqual(self.snapshot()["percent"], 33)

    def test_mismatched_complete_turn_and_duplicate_json_identity_are_rejected(self):
        self.append(self.path, record("event_msg", {"type": "task_complete", "turn_id": "another-turn"}))
        self.assertEqual(self.snapshot()["runtime_status"], "active")
        self.path.write_bytes(('{"type":"session_meta","payload":{"id":"wrong","id":"' + ID + '","source":"vscode"}}\n').encode())
        self.assertFalse(self.snapshot()["available"])

    def test_same_inode_rewrite_larger_than_cursor_drops_old_completed_plan(self):
        self.append(self.path, message(table(3)))
        service = self.service()
        self.assertEqual(self.snapshot(service)["percent"], 100)
        header = record("session_meta", {"id": ID, "source": "vscode"})
        values = [header, started("new-turn"), message(table(0)),
                  message("padding" * 1000, role="assistant", phase="analysis")]
        self.path.write_bytes(b"".join((json.dumps(v, ensure_ascii=False) + "\n").encode() for v in values))
        self.assertEqual(self.snapshot(service)["percent"], 0)

    def test_discovery_completeness_is_false_on_error_or_limit_and_true_on_full_success(self):
        service = self.service()
        self.assertEqual(len(service.discover([])), 1)
        self.assertTrue(service.discovery_complete)
        self.add_source(OTHER, [started()])
        limited = self.service(discovery_limit=1)
        limited.discover([])
        self.assertFalse(limited.discovery_complete)
        self.assertTrue(limited.discovery_truncated)
        def failed():
            raise sqlite3.OperationalError("private database path")
        broken = module.AllTaskProgress(metadata_provider=failed, codex_root=self.codex)
        self.assertEqual(broken.discover([]), [])
        self.assertFalse(broken.discovery_complete)

    def test_default_sqlite_metadata_reader_observes_committed_wal_without_writing(self):
        connection = sqlite3.connect(self.codex / "state_5.sqlite")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, cwd TEXT, title TEXT, thread_source TEXT, source TEXT, archived INTEGER, updated_at_ms INTEGER)")
        connection.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)", (ID, str(self.path), str(self.workspace), "Проверка", "user", "vscode", 0, int(NOW.timestamp()*1000)))
        connection.commit()
        service = module.AllTaskProgress(codex_root=self.codex)
        self.assertEqual(service.discover([])[0]["id"], ID)
        self.assertTrue(service.discovery_complete)
        self.assertEqual(self.snapshot(service)["percent"], 33)

    def test_new_workspace_protocol_for_a_second_public_uuid_uses_shared_projector(self):
        self.add_source(OTHER, [started()])
        folder = self.workspace / ".task-progress"
        folder.mkdir()
        (folder / (OTHER + ".json")).write_text(json.dumps(self.protocol(task_id=OTHER)), encoding="utf-8")
        value = self.snapshot(identifier=OTHER)
        self.assertEqual((value["thread_id"], value["percent"], value["progress_basis"]), (OTHER, 67, "workspace_protocol"))

    def test_uninterpretable_gap_cannot_preserve_old_hundred_percent(self):
        self.append(self.path, message(table(3)))
        service = self.service(line_bytes=4096)
        self.assertEqual(self.snapshot(service)["percent"], 100)
        with self.path.open("ab") as handle:
            handle.write(b'{"type":"event_msg","payload":' + b' ' * 5000 + b'\n')
        result = self.snapshot(service)
        self.assertIsNone(result["percent"])
        self.assertFalse(result["turn_confirmed"])
        self.append(self.path, started("confirmed-next"), message(table(1)))
        self.assertEqual(self.snapshot(service)["percent"], 33)

    def test_too_small_append_budget_is_rejected_instead_of_triggering_full_reread(self):
        with self.assertRaises(ValueError):
            self.service(append_bytes=32)

    def test_incremental_backlog_never_reports_old_complete_plan_before_reaching_tail(self):
        self.append(self.path, message(table(3)))
        service = self.service(append_bytes=512)
        self.assertEqual(self.snapshot(service)["percent"], 100)
        self.append(self.path, record("response_item", {"type": "function_call_output", "output": "x"*2500}),
                    started("latest"), message(table(1)))
        partial = self.snapshot(service)
        self.assertIsNone(partial["percent"])
        self.assertFalse(partial["turn_confirmed"])
        self.assertTrue(partial["runtime_stale"])
        for _ in range(20):
            value = self.snapshot(service)
        self.assertEqual(value["percent"], 33)
        self.assertTrue(value["turn_confirmed"])

    def test_trusted_database_extended_local_cwd_can_read_exact_workspace_protocol(self):
        folder = self.workspace / ".task-progress"
        folder.mkdir()
        (folder / (ID + ".json")).write_text(json.dumps(self.protocol()), encoding="utf-8")
        self.rows[0]["cwd"] = "\\\\?\\" + str(self.workspace)
        result = self.snapshot()
        self.assertEqual((result["percent"], result["progress_basis"]), (67, "workspace_protocol"))
        for unsafe in ["\\\\?\\UNC\\server\\share", "\\\\.\\C:\\private", "\\\\server\\share"]:
            self.rows[0]["cwd"] = unsafe
            self.assertEqual(self.snapshot()["progress_basis"], "assistant_table")

    def test_trusted_extended_local_rollout_path_can_read_current_task(self):
        self.rows[0]["rollout_path"] = "\\\\?\\" + str(self.path)
        result = self.snapshot()
        self.assertTrue(result["available"])
        self.assertEqual((result["thread_id"], result["percent"]), (ID, 33))
        for unsafe in ("\\\\?\\UNC\\server\\share\\rollout.jsonl", "\\\\.\\C:\\private\\rollout.jsonl",
                       "\\\\server\\share\\rollout.jsonl"):
            self.rows[0]["rollout_path"] = unsafe
            self.assertFalse(self.snapshot()["available"])

    def test_protocol_reported_results_are_not_claimed_as_independent_verification(self):
        folder = self.workspace / ".task-progress"
        folder.mkdir()
        protocol = self.protocol(lanes=[{"title":"Проверки", "status":"3/3 PASS", "detail":"Искусственная проверка"}])
        (folder/(ID+".json")).write_text(json.dumps(protocol),encoding="utf-8")
        result = self.snapshot()
        self.assertEqual(result["runtime_checked_at_msk"], NOW.isoformat())
        self.assertEqual(result["results"][0]["provenance"], "agent_report")
        self.assertEqual(result["results"][0]["source_at_msk"], STAMP)


if __name__ == "__main__":
    unittest.main(verbosity=2)

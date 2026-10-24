"""Isolated task projection contracts; no server, model, queue or real source writes."""
import copy
import datetime as dt
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    progress = importlib.import_module("task_progress")
except ModuleNotFoundError as exc:
    if exc.name != "task_progress":
        raise
    progress = None

THREAD = "1b566eb1-3d5b-5000-96b7-7be2779da1c3"
NOW = dt.datetime.fromisoformat("2026-10-20T20:30:00+03:00")


def snapshot(count=3, done=1):
    return {
        "task_id": THREAD, "task": "Проверка обновления",
        "overall_status": "В работе", "current": "Выполняется адресная проверка",
        "forecast": "Осталась одна проверка",
        "started_at": "2026-10-20T18:30:00+03:00",
        "updated_at": "2026-10-20T20:29:00+03:00",
        "checkpoints": [{"id": "step-" + str(n), "label": "Этап " + str(n),
                         "done": n < done} for n in range(count)],
        "lanes": [{"title": "Проверки", "status": "1/2",
                   "detail": "Одна проверка завершена"}],
        "metrics": {"completed_items": 0, "total_items": 416},
    }


class TaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(progress, "Task progress reader is missing")

    def project(self, value, **kwargs):
        return progress.project(value, THREAD, now=NOW, **kwargs)

    def test_projection_preserves_business_status_and_computes_progress(self):
        value = self.project(snapshot())
        self.assertEqual((value["schema"], value["available"], value["thread_id"]),
                         ("codex-task-progress/1", True, THREAD))
        self.assertEqual((value["done"], value["total"], value["percent"]), (1, 3, 33))
        self.assertEqual(value["title"], "Проверка обновления")
        self.assertEqual(value["status"], "В работе")
        self.assertEqual(value["current"], "Выполняется адресная проверка")
        self.assertEqual(value["forecast"], "Осталась одна проверка")
        self.assertEqual(value["checkpoints"][0], {"id": "step-0", "title": "Этап 0", "done": True})
        self.assertEqual(value["results"], [{"title": "Проверки", "status": "1/2",
                                           "detail": "Одна проверка завершена"}])
        self.assertNotIn("passed", value)
        self.assertFalse(value["final"])
        self.assertFalse(value["stale"])
        self.assertIsNone(value["finished_at_msk"])
        self.assertEqual(value["checked_at_msk"], NOW.isoformat())
        self.assertEqual(value["started_at_msk"], "2026-10-20T18:30:00+03:00")

    def test_percentage_uses_javascript_round_including_half_boundaries(self):
        for done, total, percent in [(1, 8, 13), (5, 8, 63), (1, 40, 3), (0, 3, 0), (3, 3, 100)]:
            with self.subTest(done=done, total=total):
                self.assertEqual(self.project(snapshot(total, done))["percent"], percent)

    def test_finished_old_snapshot_is_final_and_not_active_or_stale(self):
        raw = snapshot(3, 3)
        raw.update(overall_status="Завершено с ограничениями",
                   updated_at="2026-10-20T19:00:00+03:00",
                   finished_at="2026-10-20T19:00:00+03:00")
        result = self.project(raw)
        self.assertEqual((result["final"], result["running"], result["stale"], result["percent"]),
                         (True, False, False, 100))
        self.assertEqual(result["status"], "Завершено с ограничениями")

    def test_unfinished_old_snapshot_is_stale_without_replacing_business_status(self):
        raw = snapshot()
        raw["updated_at"] = "2026-10-20T19:00:00+03:00"
        result = self.project(raw)
        self.assertTrue(result["stale"])
        self.assertFalse(result["final"])
        self.assertEqual(result["status"], "В работе")

    def test_whitelist_drops_private_fields_events_and_unknown_numeric_metrics(self):
        raw = snapshot()
        raw.update(secret="owner secret", path="C:\\private", commands=["private command"],
                   events=[{"text": "private event"}])
        raw["metrics"].update(secret_number=1234, private_path="C:\\owner")
        raw["checkpoints"][0]["private"] = "owner secret"
        raw["lanes"][0]["command"] = "private command"
        result = self.project(raw)
        self.assertEqual(result["metrics"], {"completed_items": 0, "total_items": 416})
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_task_id_mismatch_and_unknown_schema_are_rejected(self):
        for changes in [{"task_id": "another-task"}, {"schema": "untrusted/9"}, {"schema": None}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.project(dict(snapshot(), **changes))

    def test_explicit_supported_source_schema_is_accepted(self):
        self.assertTrue(self.project(dict(snapshot(), schema="codex-task-status/1"))["available"])

    def test_invalid_checkpoint_flags_ids_and_counts_are_rejected(self):
        for checkpoints in [[], "bad", [{"id": "a", "label": "A", "done": 1}],
                            [{"id": "a", "label": "A", "done": False}] * 2,
                            [{"id": "../a", "label": "A", "done": True}],
                            snapshot(65)["checkpoints"]]:
            with self.subTest(checkpoints=checkpoints), self.assertRaises(ValueError):
                self.project(dict(snapshot(), checkpoints=checkpoints))

    def test_nonfinite_boolean_negative_and_oversize_metrics_are_rejected(self):
        for bad in [True, -1, float("nan"), float("inf"), "416", 2**53]:
            raw = snapshot()
            raw["metrics"]["total_items"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.project(raw)

    def test_invalid_naive_future_and_reversed_timestamps_are_rejected(self):
        for changes in [{"updated_at": "yesterday"}, {"updated_at": "2026-10-20T20:29:00"},
                        {"updated_at": "2026-10-21T20:29:00+03:00"},
                        {"started_at": "2026-10-20T20:29:30+03:00"},
                        {"finished_at": "2026-10-20T18:00:00+03:00"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.project(dict(snapshot(), **changes))

    def test_text_limits_control_characters_paths_commands_and_credentials_are_rejected(self):
        for bad in ["x" * 1201, "bad\x00value", "C:\\owner\\source.json", "/home/owner/source",
                    "powershell -Command Get-Content owner", "api_key=private-value"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.project(dict(snapshot(), current=bad))

    def test_invalid_results_are_rejected_without_inventing_test_counts(self):
        for lanes in ["bad", [{"title": "A", "status": 2, "detail": "B"}], [{}] * 33]:
            with self.subTest(lanes=lanes), self.assertRaises(ValueError):
                self.project(dict(snapshot(), lanes=lanes))

    def test_projection_does_not_modify_input(self):
        raw = snapshot()
        before = copy.deepcopy(raw)
        self.project(raw)
        self.assertEqual(raw, before)

    def fixture(self, content):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "task_status.json"
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
        return path

    def current_at(self, path, thread=THREAD):
        with patch.object(progress, "_TASKS", {THREAD: (path, THREAD)}):
            return progress.current(thread, now=NOW)

    def test_current_reads_one_allowed_file_without_mutating_it(self):
        path = self.fixture(snapshot())
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self.current_at(path)
        self.assertTrue(result["available"])
        self.assertEqual(result["percent"], 33)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["task_status.json"])

    def test_current_reloads_changed_file_and_does_not_reset_finished_progress(self):
        raw = snapshot(3, 3)
        raw["finished_at"] = raw["updated_at"]
        path = self.fixture(raw)
        for _ in range(2):
            self.assertEqual(self.current_at(path)["percent"], 100)
        raw["checkpoints"][2]["done"] = False
        raw.pop("finished_at")
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(self.current_at(path)["percent"], 67)

    def test_unknown_thread_rejected_before_any_file_read(self):
        with patch.object(Path, "open", side_effect=AssertionError("unexpected file read")):
            for unknown in ["another-task", "../task_status.json", THREAD.upper(), None, {}]:
                result = progress.current(unknown, now=NOW)
                self.assertFalse(result["available"])
                self.assertEqual(result["reason"], "unknown_thread")

    def test_missing_file_is_unavailable_and_does_not_disclose_path(self):
        path = self.fixture(snapshot())
        path.unlink()
        result = self.current_at(path)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "source_unavailable")
        self.assertNotIn(str(path), json.dumps(result))

    def test_malformed_or_duplicate_json_and_non_object_are_unavailable(self):
        for content in [b"{bad", b"[]", b'{"task_id":"first","task_id":"second"}', b'{"n":NaN}']:
            with self.subTest(content=content):
                result = self.current_at(self.fixture(content))
                self.assertFalse(result["available"])
                self.assertEqual(result["reason"], "invalid_snapshot")

    def test_oversize_file_is_rejected_without_unbounded_read(self):
        result = self.current_at(self.fixture(b" " * (256 * 1024 + 1)))
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "invalid_snapshot")

    def test_registry_identity_mismatch_is_rejected(self):
        path = self.fixture(snapshot())
        with patch.object(progress, "_TASKS", {THREAD: (path, "different-task")}):
            result = progress.current(THREAD, now=NOW)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "invalid_binding")


if __name__ == "__main__":
    unittest.main(verbosity=2)

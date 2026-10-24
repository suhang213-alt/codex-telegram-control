"""Artificial-workspace regressions for the local task progress writer."""
import contextlib
import datetime as dt
import importlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

try:
    cli = importlib.import_module("progress_cli")
except ModuleNotFoundError as error:
    if error.name != "progress_cli":
        raise
    cli = None

THREAD = "11111111-2222-4333-8444-555555555555"


class ProgressCliTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(cli, "Local progress CLI is not implemented")
        self.folder = tempfile.TemporaryDirectory(prefix="ctd_progress_cli_")
        self.addCleanup(self.folder.cleanup)
        self.workspace = Path(self.folder.name)
        self.path = self.workspace / ".task-progress" / (THREAD + ".json")

    def call(self, command, *args, workspace=None, thread=THREAD):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return cli.main([command, "--workspace", str(workspace or self.workspace),
                                 "--thread", thread, *args])
            except SystemExit as error:
                return error.code

    def init(self, *steps):
        return self.call("init", "--title", "Проверка результата", *sum((["--step", s] for s in (steps or ("Подготовить", "Проверить"))), []))

    def snapshot(self):
        return json.loads(self.path.read_text("utf-8"))

    def test_init_starts_unfinished_phase_with_stable_ids_and_msk(self):
        self.assertEqual(self.init(), 0)
        value = self.snapshot()
        self.assertEqual(value["task_id"], THREAD)
        self.assertEqual([(p["id"], p["done"]) for p in value["checkpoints"]], [("s1", False), ("s2", False)])
        self.assertIsNone(value["finished_at"])
        self.assertEqual(dt.datetime.fromisoformat(value["started_at"]).utcoffset(), dt.timedelta(hours=3))

    def test_update_completes_and_reopens_only_requested_checkpoints(self):
        self.init()
        self.assertEqual(self.call("update", "--complete", "s1", "--status", "Проверка", "--current", "Сверка", "--forecast", "Около минуты", "--result", "Код|Проверен|Искусственный пример"), 0)
        self.assertEqual([p["done"] for p in self.snapshot()["checkpoints"]], [True, False])
        self.assertEqual(self.snapshot()["lanes"], [{"title": "Код", "status": "Проверен", "detail": "Искусственный пример"}])
        self.assertEqual(self.call("update", "--pending", "s1", "--complete", "s2"), 0)
        self.assertEqual([p["done"] for p in self.snapshot()["checkpoints"]], [False, True])

    def test_finish_rejects_incomplete_checkpoints_without_changing_bytes(self):
        self.init()
        before = self.path.read_bytes()
        self.assertEqual(self.call("finish"), 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_finish_sets_time_only_after_all_checkpoints_are_closed(self):
        self.init()
        self.call("update", "--complete", "s1", "--complete", "s2")
        self.assertEqual(self.call("finish", "--current", "Проверенный результат готов"), 0)
        value = self.snapshot()
        self.assertIsNotNone(value["finished_at"])
        self.assertEqual(value["overall_status"], "Готово")
        self.assertEqual(value["finished_at"], value["updated_at"])

    def test_init_preserves_previous_bytes_and_resets_old_completion(self):
        self.init("Первый этап")
        self.call("update", "--complete", "s1")
        self.call("finish")
        before = self.path.read_bytes()
        self.assertEqual(self.init("Новая проверка"), 0)
        archives = list((self.path.parent / "history").glob(THREAD + "-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), before)
        self.assertFalse(self.snapshot()["checkpoints"][0]["done"])
        self.assertIsNone(self.snapshot()["finished_at"])

    def test_finished_phase_requires_explicit_init_before_update(self):
        self.init("Один этап")
        self.call("update", "--complete", "s1")
        self.call("finish")
        before = self.path.read_bytes()
        self.assertEqual(self.call("update", "--pending", "s1"), 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_or_conflicting_checkpoint_ids_never_change_snapshot(self):
        self.init()
        before = self.path.read_bytes()
        for args in (("--complete", "unknown"), ("--complete", "s1", "--pending", "s1")):
            with self.subTest(args=args):
                self.assertEqual(self.call("update", *args), 2)
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_display_text_and_bad_result_are_rejected_by_validator(self):
        self.assertEqual(self.call("init", "--title", "token=private", "--step", "Один этап"), 2)
        self.assertFalse(self.path.exists())
        self.init()
        before = self.path.read_bytes()
        for args in (("--current", "token=private"), ("--result", "Недостаточно|полей")):
            self.assertEqual(self.call("update", *args), 2)
            self.assertEqual(self.path.read_bytes(), before)

    def test_uuid_and_nonlocal_or_relative_workspaces_are_rejected(self):
        self.assertEqual(self.call("init", "--title", "Тест", "--step", "Этап", thread="../../other"), 2)
        for path in ("relative", r"\\server\share", r"\\?\C:\test", r"\\.\C:\test", "C:relative", str(self.workspace / "..")):
            with self.subTest(path=path):
                self.assertEqual(self.call("init", "--title", "Тест", "--step", "Этап", workspace=path), 2)
        self.assertFalse(self.path.exists())

    def test_redirected_progress_directory_is_rejected(self):
        original = Path.is_symlink
        progress = self.workspace / ".task-progress"
        progress.mkdir()
        with mock.patch.object(Path, "is_symlink", lambda p: p == progress or original(p)):
            self.assertEqual(self.init(), 2)
        self.assertFalse(self.path.exists())

    def test_cloud_placeholder_without_path_redirection_remains_usable(self):
        original = Path.lstat
        workspace = self.workspace
        def cloud_stat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == workspace:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400,
                                       st_reparse_tag=0x9000001A)
            return value
        with mock.patch.object(Path, "lstat", cloud_stat):
            self.assertEqual(self.init(), 0)
        self.assertFalse(self.snapshot()["checkpoints"][0]["done"])

    def test_atomic_replace_failure_preserves_previous_snapshot(self):
        self.init()
        before = self.path.read_bytes()
        with mock.patch.object(cli.os, "replace", side_effect=OSError("artificial replace failure")):
            self.assertEqual(self.call("update", "--complete", "s1"), 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_duplicate_or_malformed_existing_json_is_not_overwritten(self):
        self.path.parent.mkdir()
        for payload in (b'{"task_id":"one","task_id":"two"}', b'{"broken":'):
            self.path.write_bytes(payload)
            self.assertEqual(self.init(), 2)
            self.assertEqual(self.path.read_bytes(), payload)

    def test_more_than_64_steps_are_rejected_without_snapshot(self):
        self.assertEqual(self.init(*["Этап " + str(i) for i in range(65)]), 2)
        self.assertFalse(self.path.exists())

    def test_explicit_stage_percent_and_status_reach_real_projector(self):
        self.init()
        self.assertEqual(self.call("update", "--stage-percent", "s1=37.5", "--stage-status", "s1=in_progress"), 0)
        point = self.snapshot()["checkpoints"][0]
        self.assertEqual((point["percent"], point["status"], point["done"]), (37.5, "in_progress", False))
        view = cli.project(self.snapshot(), THREAD)
        self.assertEqual(view["percent"], 19)
        self.assertEqual(view["checkpoints"][0]["percent"], 37.5)

    def test_measured_stage_count_has_real_unit_and_derived_percent(self):
        self.init()
        self.assertEqual(self.call("update", "--stage-count", "s1=5/20", "--stage-unit", "s1=файлов", "--stage-status", "s1=in_progress"), 0)
        point = self.snapshot()["checkpoints"][0]
        self.assertEqual((point["count_done"], point["count_total"], point["unit"]), (5, 20, "файлов"))
        view = cli.project(self.snapshot(), THREAD)
        self.assertEqual(view["checkpoints"][0]["percent"], 25)

    def test_bad_stage_numbers_or_incomplete_quantity_preserve_snapshot(self):
        self.init()
        before = self.path.read_bytes()
        cases = [("--stage-percent", "s1=" + value) for value in ("nan", "inf", "-1", "100.1", "text")]
        cases += [("--stage-count", "s1=" + value, "--stage-unit", "s1=файлов") for value in ("1/0", "3/2", "-1/2", "nan/3", "1/a", "1/2/3")]
        cases += [("--stage-count", "s1=1/2"), ("--stage-unit", "s1=файлов"),
                  ("--stage-percent", "unknown=20"), ("--stage-status", "s1=invented"),
                  ("--stage-percent", "s1=20", "--stage-percent", "s1=30")]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.call("update", *args), 2)
                self.assertEqual(self.path.read_bytes(), before)

    def test_done_and_finish_cannot_cover_partial_stage_measurement(self):
        self.init("Проверить объём")
        self.assertEqual(self.call("update", "--stage-percent", "s1=60", "--stage-status", "s1=in_progress"), 0)
        before = self.path.read_bytes()
        self.assertEqual(self.call("update", "--complete", "s1"), 2)
        self.assertEqual(self.call("finish"), 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.call("update", "--stage-percent", "s1=100", "--stage-status", "s1=completed", "--complete", "s1"), 0)
        self.assertEqual(self.call("finish"), 0)

    def test_measured_hundred_percent_still_requires_explicit_done(self):
        self.init("Проверить объём")
        self.assertEqual(self.call("update", "--stage-count", "s1=2/2", "--stage-unit", "s1=проверок"), 0)
        self.assertFalse(self.snapshot()["checkpoints"][0]["done"])
        self.assertEqual(self.call("finish"), 2)
        self.assertEqual(self.call("update", "--complete", "s1", "--stage-status", "s1=completed"), 0)
        self.assertEqual(self.call("finish"), 0)


if __name__ == "__main__":
    unittest.main()

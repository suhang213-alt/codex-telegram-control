"""Real Windows DOS aliases must work without accepting redirected paths."""
import contextlib
import ctypes
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import all_task_progress
import progress_cli
import task_progress

THREAD = "11111111-2222-4333-8444-555555555555"


class PathRedirectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-path-policy-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.target = self.workspace / "progress.json"
        self.target.write_text("{}", encoding="utf-8")

    def test_native_metadata_rejects_redirected_ancestor(self):
        for attribute in ("is_symlink", "is_junction"):
            if not hasattr(Path, attribute):
                continue
            original = getattr(Path, attribute)
            with self.subTest(attribute=attribute), patch.object(Path, attribute,
                    lambda path: path == self.workspace or original(path)):
                with self.assertRaises(ValueError):
                    all_task_progress._normal_path(self.target)

    def test_registered_reader_rejects_junction_before_open(self):
        original = Path.is_junction
        with patch.object(Path, "is_junction", lambda path: path == self.workspace or original(path)), \
                patch.object(Path, "open", side_effect=AssertionError("Redirect must not be opened")), \
                patch.object(task_progress, "_TASKS", {THREAD: (self.target, THREAD)}):
            self.assertEqual(task_progress.current(THREAD)["reason"], "invalid_binding")

    def test_writer_rejects_junction_before_creating_progress(self):
        original = Path.is_junction
        with patch.object(Path, "is_junction", lambda path: path == self.workspace or original(path)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = progress_cli.main(["init", "--workspace", str(self.workspace), "--thread", THREAD,
                                       "--title", "Synthetic phase", "--step", "Review"])
        self.assertEqual(result, 2)
        self.assertFalse((self.workspace / ".task-progress").exists())


@unittest.skipUnless(os.name == "nt", "Windows DOS aliases require Windows")
class LocalPathAliasTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-long-workspace-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        function = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        count = function(str(self.workspace), buffer, len(buffer))
        if not count or count >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        self.alias = Path(buffer.value)
        if self.alias == self.workspace:
            self.skipTest("This filesystem does not create DOS 8.3 aliases")
        self.assertTrue(self.alias.samefile(self.workspace))

    def test_native_metadata_accepts_real_dos_alias(self):
        self.assertEqual(all_task_progress._normal_path(self.alias), self.workspace)

    def test_writer_accepts_real_dos_alias(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = progress_cli.main(["init", "--workspace", str(self.alias), "--thread", THREAD,
                                       "--title", "Synthetic phase", "--step", "Review"])
        self.assertEqual(result, 0)
        self.assertTrue((self.workspace / ".task-progress" / (THREAD + ".json")).is_file())

    def test_registered_reader_accepts_real_dos_alias(self):
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        path = self.alias / "progress.json"
        path.write_text(json.dumps({"task_id": THREAD, "task": "Synthetic phase", "overall_status": "Active",
            "started_at": timestamp, "updated_at": timestamp, "current": "Review", "forecast": "",
            "checkpoints": [{"id": "one", "label": "Review", "done": False}], "lanes": [], "metrics": {}}), encoding="utf-8")
        with patch.object(task_progress, "_TASKS", {THREAD: (path, THREAD)}):
            self.assertTrue(task_progress.current(THREAD)["available"])


if __name__ == "__main__":
    unittest.main()

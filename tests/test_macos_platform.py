"""Synthetic macOS adapter tests: no real app, keychain, Telegram, or engine."""
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from codex_telegram import macos_platform as platform
from codex_telegram import macos_cli
from codex_telegram.config import SafeError


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bundle = self.root / "Synthetic Desktop.app"
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.home = self.root / "bridge"
        self.executable = self.bundle / "Contents/MacOS/SyntheticDesktop"
        self.real_cli = self.bundle / platform.CLI_RESOURCE
        for path in (self.executable, self.real_cli):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic executable; never launched")
            os.chmod(path, 0o755)
        self.info = self.bundle / "Contents/Info.plist"
        self.write_info()
        # The filesystem fixture models macOS executable permission bits on a
        # Windows test host. Native macOS APIs are always replaced explicitly.
        original_stat = Path.stat
        executables = {self.executable, self.real_cli}
        def fixture_stat(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path in executables:
                values = list(result)
                values[0] = stat.S_IFREG | 0o755
                return os.stat_result(values)
            return result
        self.addCleanup(patch.stopall)
        patch.object(Path, "stat", fixture_stat).start()
        patch.object(platform, "require_macos", lambda: None).start()
        self.commands = []
        def command(args, **_kwargs):
            self.commands.append(args)
            details = f"Identifier={platform.BUNDLE_ID}\nTeamIdentifier={platform.TEAM_ID}\n"
            return SimpleNamespace(returncode=0, stdout="", stderr=details if "-dv" in args else "")
        patch.object(platform, "_command", side_effect=command).start()

    def write_info(self, **changes):
        value = {"CFBundleIdentifier": platform.BUNDLE_ID, "CFBundleExecutable": "SyntheticDesktop",
                 "CFBundleShortVersionString": "0.0-synthetic"}
        value.update(changes)
        self.info.write_bytes(plistlib.dumps(value))

    def config(self):
        return {"schema": 1, "backend": "desktop", "desktop": dict(
            platform.inspect_bundle(self.bundle), codex_home=str(self.profile))}

    def test_signed_bundle_has_expected_original_engine(self):
        identity = platform.inspect_bundle(self.bundle)
        self.assertEqual(identity["real_cli"], str(self.real_cli))
        self.assertEqual(identity["team_id"], platform.TEAM_ID)
        verify = self.commands[0]
        self.assertEqual(verify[:5], ["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R"])
        self.assertIn('anchor apple generic', verify[5])
        self.assertIn(platform.TEAM_ID, verify[5])

    def test_foreign_bundle_identifier_rejected_before_codesign(self):
        self.write_info(CFBundleIdentifier="org.example.synthetic")
        with self.assertRaises(SafeError):
            platform.inspect_bundle(self.bundle)
        self.assertFalse(self.commands)

    def test_path_traversal_executable_rejected(self):
        self.write_info(CFBundleExecutable="../Other")
        with self.assertRaises(SafeError):
            platform.inspect_bundle(self.bundle)

    def test_script_cannot_substitute_for_macho_engine(self):
        self.real_cli.write_bytes(b"#!/bin/sh\nexit 0\n")
        with self.assertRaises(SafeError):
            platform.inspect_bundle(self.bundle)

    def test_invalid_signature_cannot_be_overridden_by_display_metadata(self):
        platform._command.return_value = SimpleNamespace(returncode=1, stdout="", stderr="")
        platform._command.side_effect = None
        with self.assertRaises(SafeError):
            platform.inspect_bundle(self.bundle)

    def test_foreign_signer_rejected(self):
        def result(args):
            return SimpleNamespace(returncode=0, stdout="", stderr=(
                f"Identifier={platform.BUNDLE_ID}\nTeamIdentifier=EXAMPLE\n" if "-dv" in args else ""))
        platform._command.side_effect = result
        with self.assertRaises(SafeError):
            platform.inspect_bundle(self.bundle)

    def test_engine_update_requires_reconfiguration(self):
        config = self.config()
        self.real_cli.write_bytes(b"\xcf\xfa\xed\xfechanged synthetic engine")
        with self.assertRaises(SafeError):
            platform.verify_desktop(config)

    def test_app_version_change_requires_reconfiguration(self):
        config = self.config()
        self.write_info(CFBundleShortVersionString="0.1-synthetic")
        with self.assertRaises(SafeError):
            platform.verify_desktop(config)

    def test_parent_must_be_same_user_and_exact_executable(self):
        config = self.config()
        with patch.object(platform, "_process_table", return_value=[(42, 501, "ignored")]), \
                patch.object(platform.os, "getuid", return_value=501, create=True), \
                patch.object(platform, "process_path", return_value=self.executable):
            self.assertTrue(platform.validate_parent(config, 42))
        with patch.object(platform, "_process_table", return_value=[(42, 502, "ignored")]), \
                patch.object(platform.os, "getuid", return_value=501, create=True), \
                patch.object(platform, "process_path", return_value=self.executable):
            with self.assertRaises(SafeError):
                platform.validate_parent(config, 42)
        with patch.object(platform, "_process_table", return_value=[(42, 501, "ignored")]), \
                patch.object(platform.os, "getuid", return_value=501, create=True), \
                patch.object(platform, "process_path", return_value=self.real_cli):
            with self.assertRaises(SafeError):
                platform.validate_parent(config, 42)

    def test_same_short_process_name_does_not_establish_identity(self):
        config = self.config()
        with patch.object(platform, "_process_table", return_value=[(42, 501, "SyntheticDesktop")]), \
                patch.object(platform.os, "getuid", return_value=501, create=True), \
                patch.object(platform, "process_path") as native:
            self.assertEqual(platform.running_desktop(config), [])
            native.assert_not_called()

    def test_running_desktop_prevents_any_child_launch(self):
        config = self.config()
        with patch.object(platform, "_launch_lock", return_value=nullcontext()), \
                patch.object(platform, "running_desktop", return_value=[42]), \
                patch.object(platform.subprocess, "Popen") as child:
            with self.assertRaises(SafeError):
                platform.launch(config, self.home)
            child.assert_not_called()
            self.assertFalse((self.home / "desktop-engine-shim.sh").exists())

    def test_environment_removes_credentials_and_uses_existing_profile(self):
        config = self.config()
        source = {"PATH": "/usr/bin", "TELEGRAM_BOT_TOKEN": "synthetic",
                  "OPENAI_API_KEY": "synthetic", "AWS_SECRET_ACCESS_KEY": "synthetic",
                  "CODEX_APP_TOOLS_PIPE_PATH": "other-chat", "CODEX_THREAD_ID": "synthetic",
                  "CODEX_CLI_PATH": "old", "PYTHONPATH": "untrusted", "DYLD_INSERT_LIBRARIES": "untrusted"}
        with patch.dict(os.environ, source, clear=True):
            result = platform.launch_environment(config, self.home / "shim.sh")
        self.assertEqual(set(result), {"PATH", "CODEX_HOME", "CODEX_CLI_PATH"})
        self.assertEqual(result["CODEX_HOME"], str(self.profile))
        self.assertNotIn("codex", Path(result["CODEX_HOME"]).parts[-1:])

    def test_prepare_launcher_quotes_spaces_and_does_not_launch(self):
        config = self.config()
        with patch.object(platform.subprocess, "Popen") as child:
            shim = platform.prepare_launch(config, self.home / "path with ' quote")
            content = shim.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("#!/bin/sh\nexec "))
            self.assertTrue(content.endswith(' "$@"\n'))
            self.assertIn("'\"'\"'", content)
            metadata = json.loads((shim.parent / ".native-launch.json").read_text())
            self.assertEqual(metadata["shim_sha256"], platform.file_hash(shim))
            self.assertEqual(metadata["desktop"]["real_cli"], str(self.real_cli))
            child.assert_not_called()

    def test_launcher_keeps_virtualenv_interpreter_path(self):
        config = self.config()
        venv_python = self.root / "venv/bin/python"
        base_python = self.root / "base/python"
        for path in (venv_python, base_python):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic interpreter, never executed")
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return base_python if path == venv_python else original_resolve(path, *args, **kwargs)
        # Model the symlink without requiring Windows symlink privileges.
        with patch.object(platform.sys, "executable", str(venv_python)), patch.object(Path, "resolve", resolve):
            shim = platform.prepare_launch(config, self.home)
        metadata = json.loads((self.home / ".native-launch.json").read_text())
        self.assertEqual(metadata["python"], str(venv_python))
        self.assertNotIn(str(base_python), shim.read_text())

    def test_rollback_launches_original_app_without_gateway(self):
        config = self.config()
        with patch.object(platform, "_launch_lock", return_value=nullcontext()), \
                patch.object(platform, "running_desktop", return_value=[]), \
                patch.object(platform.subprocess, "Popen", return_value=SimpleNamespace(pid=42)) as child, \
                patch.object(platform, "prepare_launch") as prepare:
            result = platform.launch(config, self.home, rollback=True)
            prepare.assert_not_called()
        self.assertEqual(child.call_args.args[0], [str(self.executable)])
        self.assertNotIn("CODEX_CLI_PATH", child.call_args.kwargs["env"])
        self.assertEqual(child.call_args.kwargs["env"]["CODEX_HOME"], str(self.profile))
        self.assertFalse(result["gateway_requested"])
        self.assertFalse(result["desktop_connection_verified"])

    def test_launch_requests_app_not_a_second_engine(self):
        config = self.config()
        with patch.object(platform, "_launch_lock", return_value=nullcontext()), \
                patch.object(platform, "running_desktop", return_value=[]), \
                patch.object(platform.subprocess, "Popen", return_value=SimpleNamespace(pid=42)) as child:
            result = platform.launch(config, self.home)
        self.assertEqual(child.call_args.args[0], [str(self.executable)])
        self.assertTrue(child.call_args.kwargs["start_new_session"])
        self.assertIn("CODEX_CLI_PATH", child.call_args.kwargs["env"])
        self.assertFalse(result["desktop_connection_verified"])

    def test_setup_existing_configuration_is_never_overwritten(self):
        self.home.mkdir()
        existing = self.home / "config.json"
        existing.write_text("synthetic original")
        with patch.object(macos_cli, "input", create=True) as prompt:
            with self.assertRaises(SafeError):
                macos_cli.setup(self.home)
            prompt.assert_not_called()
        self.assertEqual(existing.read_text(), "synthetic original")

    def test_keychain_failure_never_falls_back_to_file(self):
        with patch.object(macos_cli, "secure_keyring", side_effect=SafeError("unavailable")):
            with self.assertRaises(SafeError):
                macos_cli._save_token(self.home, "123456789:" + "x" * 25)
        self.assertFalse(self.home.exists())

    def test_setup_uses_existing_profile_without_reading_or_copying_it(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        synthetic_profile_file = self.profile / "synthetic-owner-state"
        synthetic_profile_file.write_text("never read by setup")
        answers = ["424242", str(workspace), "https://panel.example.org", str(self.profile)]
        with patch("builtins.input", side_effect=answers), \
                patch.object(macos_cli.getpass, "getpass", return_value=""), \
                patch.object(macos_cli, "_save_token") as save_secret, \
                patch("sys.stdout", new_callable=io.StringIO):
            macos_cli.setup(self.home, app_bundle=str(self.bundle))
        saved = json.loads((self.home / "config.json").read_text())
        self.assertEqual(saved["backend"], "desktop")
        self.assertEqual(saved["desktop"]["codex_home"], str(self.profile))
        self.assertEqual(set(p.name for p in self.home.iterdir()), {"config.json"})
        save_secret.assert_not_called()

    def test_doctor_requires_completed_original_engine_handshake(self):
        config = self.config()
        from codex_telegram.desktop_rpc import DesktopRpc
        with patch.object(platform, "running_desktop", return_value=[42]), \
                patch.object(DesktopRpc, "read_status", return_value={"backend": "desktop", "connected": True, "ready": False}), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(macos_cli.doctor(config, self.home), 2)
        with patch.object(platform, "running_desktop", return_value=[42]), \
                patch.object(DesktopRpc, "read_status", return_value={"backend": "desktop", "connected": True, "ready": True}), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(macos_cli.doctor(config, self.home), 0)
        with patch.object(platform, "running_desktop", return_value=[42]), \
                patch.object(DesktopRpc, "read_status", return_value={"backend": "standalone", "connected": True, "ready": True}), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(macos_cli.doctor(config, self.home), 2)


class OtherHostTests(unittest.TestCase):
    def test_platform_gate_cannot_launch_on_other_hosts(self):
        with patch.object(platform.sys, "platform", "linux"), \
                patch.object(platform.subprocess, "Popen") as child:
            with self.assertRaises(SafeError):
                platform.require_macos()
            child.assert_not_called()

    def test_desktop_default_home_is_separate_from_standalone_core(self):
        with patch.dict(os.environ, {"CODEX_TELEGRAM_HOME": ""}), \
                patch.object(Path, "home", return_value=Path("/synthetic-home")):
            self.assertEqual(platform.desktop_home().name, "codex-telegram-desktop")

    def test_cli_help_does_not_require_a_platform_or_config(self):
        with patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(SystemExit) as result:
            macos_cli.main(["--help"])
        self.assertEqual(result.exception.code, 0)


if __name__ == "__main__":
    unittest.main()

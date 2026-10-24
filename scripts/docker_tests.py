"""Offline setup regressions; optional real Docker smoke uses synthetic files only."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("docker_setup_helper", ROOT / "scripts" / "docker.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-fixture-")
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "private"
        self.native = self.base / "native"
        self.native.mkdir()
        self.values = {"schema": helper.SCHEMA, "host": "macos", "native_home": str(self.native),
                       "owner_id": 123456789, "public_url": "https://panel.example.org",
                       "image": "example/desktop-relay:0.0.0-fixture", "cloudflared_image": helper.CLOUDFLARED,
                       "uid": 10001, "gid": 10001}

    def tearDown(self):
        self.temp.cleanup()

    def test_settings_cannot_enter_repository_profile_or_home(self):
        for path in (ROOT, ROOT / "secrets", ROOT.parent, Path.home(), self.base / ".codex" / "relay"):
            with self.subTest(path=path.name), self.assertRaises(helper.SetupError):
                helper.private_home(path)

    def test_redirected_directory_is_rejected(self):
        target = self.home
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda item: item == target or original(item)):
            with self.assertRaises(helper.SetupError):
                helper.private_home(target)

    def test_secrets_are_exclusive_and_private(self):
        path = self.base / "fixture-secret"
        helper.write_new_private(path, "fixture\n")
        with self.assertRaises(FileExistsError):
            helper.write_new_private(path, "replacement")
        self.assertEqual(path.read_text(), "fixture\n")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_nonruntime_or_injected_image(self):
        for value in ("example/relay", "example/relay:latest", "example/relay:package-check", "example/relay:tag;echo", None):
            with self.subTest(value=value), self.assertRaises(helper.SetupError):
                helper.validate_image(value)
        self.assertEqual(helper.validate_image(self.values["image"]), self.values["image"])
        self.assertEqual(helper.validate_image(helper.CLOUDFLARED), helper.CLOUDFLARED)

    def test_origin_rejects_credentials_and_paths(self):
        for value in ("http://panel.example.org", "https://user:password@panel.example.org", "https://panel.example.org/a", "https://localhost", None):
            with self.subTest(value=value), self.assertRaises(helper.SetupError):
                helper.validate_origin(value)

    def test_setup_does_not_overwrite_existing_directory(self):
        self.home.mkdir()
        (self.home / "keep").write_text("original")
        with patch.object(helper, "host_kind", return_value="macos"), self.assertRaises(helper.SetupError):
            helper.setup(self.home)
        self.assertEqual((self.home / "keep").read_text(), "original")

    def test_setup_rejects_even_empty_existing_directory(self):
        self.home.mkdir()
        with patch.object(helper, "host_kind", return_value="macos"), self.assertRaises(helper.SetupError):
            helper.setup(self.home)

    def test_private_directory_permissions(self):
        path = self.base / "acl-fixture"
        helper.protect_directory(path)
        if os.name == "nt":
            helper.check_windows_acl(path)
            result = subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:(R)"], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            with self.assertRaises(helper.SetupError):
                helper.check_windows_acl(path)
        else:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_invalid_secret_writes_no_files(self):
        with patch.object(helper, "host_kind", return_value="macos"), \
             patch.object(helper, "native_home", return_value=self.native), \
             patch.object(helper, "native_metadata", return_value=(123456789, "https://panel.example.org")), \
             patch.object(helper, "docker_available", return_value="docker"), \
             patch("builtins.input", return_value=self.values["image"]), \
             patch.object(helper.getpass, "getpass", return_value="invalid"), \
             self.assertRaises(helper.SetupError):
            helper.setup(self.home)
        self.assertFalse(self.home.exists())

    def test_synthetic_setup_roundtrip_and_owner_change_gate(self):
        kind = "windows" if os.name == "nt" else "macos"
        native_value = ({"schema": "codex-telegram-desktop/1", "owner_user_id": 123456789}
                        if kind == "windows" else {"backend": "desktop", "owner_id": 123456789,
                                                  "public_url": "https://panel.example.org"})
        native_path = self.native / "config.json"
        native_path.write_text(json.dumps(native_value))
        answers = (["https://panel.example.org", self.values["image"]] if kind == "windows" else [self.values["image"]])
        with patch.object(helper, "host_kind", return_value=kind), \
             patch.object(helper, "native_home", return_value=self.native), \
             patch.object(helper, "docker_available", return_value="docker"), \
             patch("builtins.input", side_effect=answers), patch("builtins.print"), \
             patch.object(helper.getpass, "getpass", side_effect=["123456789:" + "A" * 35, "fixture" * 8]):
            helper.setup(self.home)
            loaded = helper.load_settings(self.home)
            self.assertEqual(loaded["owner_id"], 123456789)
            self.assertEqual(set(path.name for path in (self.home / "secrets").iterdir()), set(helper.SECRET_NAMES))
            self.assertRegex((self.home / "secrets" / "connector_key").read_text().strip(), r"^[0-9a-f]{64}$")
            native_value["owner_user_id" if kind == "windows" else "owner_id"] = 987654321
            native_path.write_text(json.dumps(native_value))
            with self.assertRaises(helper.SetupError):
                helper.load_settings(self.home)

    def test_compose_environment_contains_paths_not_secret_contents(self):
        with patch.dict(os.environ, {}, clear=True):
            env = helper.compose_environment(self.home, self.values)
        self.assertEqual(env["CTD_DOCKER_HOME"], self.home.as_posix())
        self.assertFalse(any("TOKEN" in key or "KEY" in key for key in env))

    def test_host_receives_file_reference_and_fixed_native_command(self):
        env = helper.host_environment(self.home, self.values)
        self.assertEqual(env["CTD_RELAY_KEY_FILE"], str(self.home / "secrets" / "connector_key"))
        self.assertEqual(helper.native_command("macos", "run"), [sys.executable, "-m", "codex_telegram.macos_cli", "run"])
        self.assertNotIn("CTD_RELAY_URL", env)

    def test_secret_arguments_are_file_paths(self):
        args = helper.relay_args(self.values)
        self.assertEqual(args[args.index("--key-file") + 1], "/run/secrets/connector_key")
        self.assertEqual(args[args.index("--token-file") + 1], "/run/secrets/telegram_bot_token")
        self.assertNotIn("--token", args)

    def test_context_is_exact_allowlist(self):
        rules = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual(rules[0], "**")
        for rule in rules[1:]:
            self.assertTrue(rule.startswith("!"))
            self.assertFalse(any(mark in rule for mark in ("*", "?", "[", "..")))
        included = {row[1:] for row in rules if row.startswith("!") and not row.endswith("/")}
        self.assertNotIn("src/.env", included)
        self.assertNotIn("src/codex_telegram/config.json", included)
        self.assertNotIn("MANIFEST.json", included)
        for path in (ROOT / "src" / "codex_telegram").glob("*.py"):
            self.assertIn(path.relative_to(ROOT).as_posix(), included)

    def test_runtime_has_nonroot_user_and_no_dependencies_or_source_tree(self):
        text = (ROOT / "Dockerfile").read_text()
        runtime = text.split(" AS runtime\n", 1)[1]
        self.assertIn("USER 10001:10001", runtime)
        self.assertIn("COPY --from=package-build /opt/package /opt/package", runtime)
        self.assertNotIn("COPY src", runtime)
        self.assertNotIn("pip install", runtime)
        self.assertIn("--no-deps", text)
        self.assertIn("@sha256:", text)


def docker_smoke(image):
    """Run only the candidate relay; no Desktop, Telegram, tunnel or model connection."""
    def call(args, timeout=30):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError("Docker smoke command failed: " + args[0])
        return result.stdout.strip()
    with tempfile.TemporaryDirectory(prefix="relay-container-fixture-") as directory:
        base = Path(directory).resolve()
        secret_dir, state = base / "secrets", base / "state"
        secret_dir.mkdir(mode=0o700); state.mkdir(mode=0o700)
        helper.write_new_private(secret_dir / "telegram_bot_token", "123456789:" + "A" * 35 + "\n")
        helper.write_new_private(secret_dir / "connector_key", "ab" * 32 + "\n")
        helper.write_new_private(secret_dir / "cloudflare_tunnel_token", "fixture-only" * 5 + "\n")
        uid = os.getuid() if hasattr(os, "getuid") else 10001
        gid = os.getgid() if hasattr(os, "getgid") else 10001
        if uid == 0 or gid == 0:
            raise RuntimeError("Run container smoke as an ordinary non-root host user.")
        values = {"image": image, "public_url": "https://panel.example.org", "owner_id": 123456789,
                  "uid": uid, "gid": gid, "cloudflared_image": helper.CLOUDFLARED}
        env = helper.compose_environment(base, values)
        rendered = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.yml"), "config", "--format", "json"],
                                  env=env, capture_output=True, text=True, timeout=30, check=True)
        config = json.loads(rendered.stdout)
        relay = config["services"]["relay"]
        assert relay["read_only"] is True and relay["user"] == f"{uid}:{gid}"
        assert all(port["host_ip"] == "127.0.0.1" and port["target"] == 8788 for port in relay["ports"])
        assert not config["services"]["tunnel"].get("ports")
        assert set(item["source"] for item in relay["secrets"]) == {"telegram_bot_token", "connector_key"}
        assert {item["source"] for item in config["services"]["tunnel"]["secrets"]} == {"cloudflare_tunnel_token"}
        assert not relay.get("privileged") and not relay.get("use_api_socket")
        flags = ["--network", "none", "--read-only", "--user", f"{uid}:{gid}", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges=true", "--pids-limit", "128", "--memory", "512m",
                 "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777"]
        for name in ("telegram_bot_token", "connector_key"):
            flags += ["--mount", f"type=bind,source={secret_dir / name},target=/run/secrets/{name},readonly"]
        flags += ["--mount", f"type=bind,source={state},target=/data"]
        args = helper.relay_args(values)
        call(["run", "--rm", *flags, image, *args, "--self-check"])
        container = call(["run", "--detach", *flags, image, *args])
        if not re.fullmatch(r"[a-f0-9]{64}", container):
            raise RuntimeError("Unexpected Docker container identifier.")
        try:
            probe = "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=2)); assert r.get('ok') is True and r.get('service')=='codex-telegram-relay' and r.get('host_connected') is False"
            for attempt in range(20):
                result = subprocess.run(["docker", "exec", container, "python", "-c", probe], capture_output=True, timeout=5)
                if result.returncode == 0:
                    break
                time.sleep(.25)
            else:
                raise RuntimeError("Relay did not become live in the isolated container.")
            inspection = json.loads(call(["inspect", container]))[0]
            assert inspection["HostConfig"]["ReadonlyRootfs"] is True
            assert inspection["HostConfig"]["NetworkMode"] == "none"
            assert inspection["Config"]["User"] == f"{uid}:{gid}"
            mounts = {m["Destination"] for m in inspection["Mounts"]}
            required = {"/run/secrets/telegram_bot_token", "/run/secrets/connector_key", "/data"}
            assert required.issubset(mounts) and mounts.issubset(required | {"/tmp"})
            assert "/tmp" in inspection["HostConfig"]["Tmpfs"]
            print(json.dumps({"container_smoke": "passed", "network": "none", "host_connected": False,
                              "nonroot": True, "read_only": True, "secrets": "synthetic", "live_telegram": False}))
        finally:
            call(["rm", "--force", container])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--container-smoke", action="store_true")
    parser.add_argument("--image", default="codex-telegram-control:runtime-smoke")
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SetupTests))
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.container_smoke:
        docker_smoke(args.image)

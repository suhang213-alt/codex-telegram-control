"""Docker hooks on the Windows adapter; no live Telegram, Desktop, or model."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
import telegram_bridge as bridge
import miniapp_server
from codex_telegram import relay_client
from codex_telegram.config import SafeError


class WindowsRelayHooksTests(unittest.TestCase):
    def test_api_uses_container_and_never_falls_back(self):
        fake = Mock()
        with patch.dict(os.environ, {"CTD_RELAY_KEY_FILE": "synthetic"}), \
                patch.object(relay_client, "current", return_value=fake), \
                patch.object(bridge.urllib.request, "urlopen", side_effect=AssertionError("Direct Telegram forbidden")) as direct:
            bridge.api("getUpdates", {"offset": 123})
            fake.telegram.assert_called_once_with("getUpdates", {"offset": 123})
            direct.assert_not_called()
        with patch.dict(os.environ, {"CTD_RELAY_KEY_FILE": "synthetic"}), \
                patch.object(relay_client, "current", side_effect=SafeError("relay offline")), \
                patch.object(bridge.urllib.request, "urlopen", side_effect=AssertionError("Direct Telegram forbidden")):
            with self.assertRaises(SafeError): bridge.api("getMe", {})

    def test_attachment_download_uses_fixed_container_file_transport(self):
        fake = Mock(); fake.download.return_value = b"synthetic attachment"
        with patch.dict(os.environ, {"CTD_RELAY_KEY_FILE": "synthetic"}), \
                patch.object(relay_client, "current", return_value=fake), \
                patch.object(bridge, "api", return_value={"file_path": "documents/fixture.txt"}), \
                patch.object(miniapp_server, "store_file", return_value={"id": "synthetic"}) as store, \
                patch.object(bridge.urllib.request, "urlopen", side_effect=AssertionError("Direct Telegram forbidden")):
            self.assertEqual(bridge.download_attachment("fixture-id", "fixture.txt"), {"id": "synthetic"})
            fake.download.assert_called_once_with("documents/fixture.txt")
            store.assert_called_once_with("fixture.txt", b"synthetic attachment")

    def test_voice_download_is_relayed_without_executing_a_model(self):
        fake = Mock(); fake.download.return_value = b"synthetic audio"
        def transcribe(arguments, **kwargs):
            self.assertEqual(Path(arguments[-1]).read_bytes(), b"synthetic audio")
            return SimpleNamespace(stdout=json.dumps({"text": "synthetic transcription"}))
        with patch.dict(os.environ, {"CTD_RELAY_KEY_FILE": "synthetic"}), \
                patch.object(relay_client, "current", return_value=fake), \
                patch.object(bridge, "token", return_value="synthetic token, never used"), \
                patch.object(bridge, "api", return_value={"file_path": "voice/fixture.ogg"}), \
                patch.object(bridge.subprocess, "run", side_effect=transcribe) as model, \
                patch.object(bridge.urllib.request, "urlopen", side_effect=AssertionError("Direct Telegram forbidden")):
            self.assertEqual(bridge.transcribe_voice("fixture-id"), "synthetic transcription")
            fake.download.assert_called_once_with("voice/fixture.ogg")
            self.assertEqual(model.call_count, 1)

    def test_previous_cloud_mode_is_rejected_before_join_or_startup(self):
        with patch.dict(os.environ, {"CTD_RELAY_KEY_FILE": "synthetic"}), \
                patch.object(bridge.config, "validate"), patch.object(bridge.config, "settings", return_value={}), \
                patch.object(bridge, "token", return_value="synthetic"), \
                patch.object(bridge, "cloud_transport_enabled", return_value=True), \
                patch.object(relay_client, "start") as start:
            with self.assertRaises(RuntimeError): bridge.run()
            start.assert_not_called()


if __name__ == "__main__": unittest.main()

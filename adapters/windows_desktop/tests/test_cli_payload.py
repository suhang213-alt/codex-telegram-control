import contextlib
import io
import json
import unittest
import urllib.parse
from unittest.mock import Mock, patch

import cli_payload
import telegram_bridge as bridge


class CliPayloadTests(unittest.TestCase):
    def test_large_history_exceeds_windows_argv_limit_but_reaches_handler_via_stdin(self):
        payload = {"id": "synthetic-thread", "messages": [{"id": "m1", "role": "assistant", "text": "я" * 40000}], "nextCursor": None}
        encoded = urllib.parse.quote(json.dumps(payload, ensure_ascii=False), safe="")
        self.assertGreater(len(encoded), 96500)
        with patch.object(bridge.sys, "argv", ["telegram_bridge.py", "cache_thread", "-"]), \
             patch.object(bridge.sys, "stdin", io.StringIO(encoded)), patch.object(bridge, "ensure_dirs"), \
             patch.object(bridge, "cache_snapshot") as cache, contextlib.redirect_stdout(io.StringIO()) as output:
            bridge.main()
        cache.assert_called_once_with("cache_thread", encoded)
        self.assertNotIn("я", output.getvalue())
        self.assertNotIn(encoded, output.getvalue())

    def test_encoded_and_decoded_limits_fail_before_private_state_or_cache_writes(self):
        payloads = ["x" * (cli_payload.MAX_ENCODED_BYTES + 1),
                    urllib.parse.quote(json.dumps({"text": "x" * cli_payload.MAX_DECODED_BYTES}), safe="")]
        for encoded in payloads:
            with patch.object(bridge.sys, "argv", ["telegram_bridge.py", "cache_thread", "-"]), \
                 patch.object(bridge.sys, "stdin", io.StringIO(encoded)), patch.object(bridge, "ensure_dirs") as ensure, \
                 patch.object(bridge, "cache_snapshot") as cache, contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as result:
                    bridge.main()
            self.assertEqual(result.exception.code, 2)
            self.assertEqual(json.loads(output.getvalue()), {"ok": False, "error": "invalid_stdin_payload"})
            ensure.assert_not_called()
            cache.assert_not_called()

    def test_stdin_contract_is_bounded_noninteractive_and_strict_utf8_uri(self):
        for value in ("", "%ZZ", "%FF", "не-кодировано"):
            with self.assertRaises((ValueError, UnicodeError)):
                cli_payload.read_uri_stdin(io.StringIO(value))
        source = Mock()
        source.isatty.return_value = True
        with self.assertRaises(ValueError):
            cli_payload.read_uri_stdin(source)
        source.read.assert_not_called()
        self.assertTrue(all(cli_payload.supports_stdin(action) for action in
                            ("cache_threads", "cache_thread_page", "reply", "receive_desktop", "notice", "checkpoint")))
        self.assertFalse(cli_payload.supports_stdin("ack"))


if __name__ == "__main__":
    unittest.main()

"""Independent relay boundary tests using synthetic owners and offline peers."""
import base64
import copy
import hashlib
import hmac
from http.client import HTTPMessage
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_telegram.config import SafeError
from codex_telegram import relay_protocol as protocol
from codex_telegram.relay_server import NoRedirect, Relay, handler, telegram_http
from codex_telegram import host_connector


KEY = bytes.fromhex("ab" * 32)
TOKEN = "123456:" + "synthetic_token_for_offline_security_tests"
OWNER = 4242
ORIGIN = "https://panel.example.org"


def init_data(owner=OWNER, *, age=0, token=TOKEN):
    fields = {"auth_date": str(int(time.time()) - age),
              "user": json.dumps({"id": owner}, separators=(",", ":"))}
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class Clock:
    def __init__(self): self.value = 1000.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class RelayEnvelopeSecurityTests(unittest.TestCase):
    def test_replay_and_cross_channel_are_rejected(self):
        clock = Clock()
        verifier = protocol.Verifier(KEY, clock=clock)
        envelope = protocol.signed(KEY, "request:/poll", {"lease": "synthetic"}, now=clock())
        with self.assertRaises(SafeError): verifier.verify("request:/complete", envelope)
        self.assertEqual(verifier.verify("request:/poll", envelope), {"lease": "synthetic"})
        with self.assertRaises(SafeError): verifier.verify("request:/poll", envelope)

    def test_payload_tampering_and_wrong_key_are_rejected(self):
        envelope = protocol.signed(KEY, "request:/poll", {"owner": OWNER}, now=1000)
        changed = copy.deepcopy(envelope)
        changed["payload"]["owner"] = OWNER + 1
        for key, value in ((KEY, changed), (b"different-secret", envelope)):
            with self.subTest(key_matches=key == KEY), self.assertRaises(SafeError):
                protocol.Verifier(key, clock=lambda: 1000).verify("request:/poll", value)

    def test_expired_future_and_boolean_timestamps_are_rejected(self):
        for stamp in (900, 1010, True):
            with self.subTest(stamp=stamp), self.assertRaises(SafeError):
                envelope = protocol.signed(KEY, "request:/join", {}, now=stamp)
                if stamp is True:
                    envelope["at"] = True
                    content = {key: value for key, value in envelope.items() if key != "mac"}
                    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    envelope["mac"] = hmac.new(KEY, b"request:/join\n" + raw, hashlib.sha256).hexdigest()
                protocol.Verifier(KEY, clock=lambda: 1000).verify("request:/join", envelope)

    def test_response_must_match_request_nonce(self):
        envelope = protocol.signed(KEY, "response:/poll", {"job": None}, nonce="a" * 48, now=1000)
        verifier = protocol.Verifier(KEY, clock=lambda: 1000)
        with self.assertRaises(SafeError):
            verifier.verify("response:/poll", envelope, expected_nonce="b" * 48)
        self.assertEqual(verifier.verify("response:/poll", envelope, expected_nonce="a" * 48), {"job": None})

    def test_binary_transport_preserves_every_byte_and_rejects_excess(self):
        original = bytes(range(256)) + "Синтетический отчёт\x00".encode()
        self.assertEqual(protocol.unpack_body(protocol.pack_body(original)), original)
        with patch.object(protocol, "MAX_BODY", 8):
            self.assertEqual(protocol.unpack_body(protocol.pack_body(b"12345678")), b"12345678")
            with self.assertRaises(SafeError): protocol.pack_body(b"123456789")
            with self.assertRaises(SafeError): protocol.unpack_body(base64.b64encode(b"123456789").decode())
        for invalid in ("!!!!", "YQ==\n", 123):
            with self.subTest(invalid=invalid), self.assertRaises(SafeError): protocol.unpack_body(invalid)

    def test_hop_credentials_and_cookie_headers_are_not_forwarded(self):
        headers = protocol.filter_headers({"Host": "evil.example", "Authorization": "secret",
                    "Cookie": "secret", "Proxy-Authorization": "secret", "X-Forwarded-Host": "evil.example",
                    "Content-Length": "999", "Transfer-Encoding": "chunked", "Accept-Encoding": "gzip",
                    "Content-Type": "application/json", "X-Telegram-Init-Data": "synthetic"})
        self.assertEqual(headers, {"content-type": "application/json", "x-telegram-init-data": "synthetic"})
        response = protocol.filter_headers({"Set-Cookie": "secret", "Location": "https://evil.example",
                                            "Content-Disposition": "attachment; filename=test.txt"}, response=True)
        self.assertEqual(response, {"content-disposition": "attachment; filename=test.txt"})

    def test_header_injection_and_multi_ranges_are_rejected(self):
        for headers in ({"Content-Type": "text/plain\r\nX-Injected: yes"},
                        {"Range": "bytes=0-1,4-8"}, {"Range": "bytes=0-9999999999999"}):
            with self.subTest(headers=headers), self.assertRaises(SafeError): protocol.filter_headers(headers)
        self.assertEqual(protocol.filter_headers({"Range": "bytes=0-1"}), {"range": "bytes=0-1"})

    def test_ssrf_absolute_encoded_and_internal_routes_are_rejected(self):
        targets = ["http://169.254.169.254/latest/meta-data", "//evil.example/api/state",
                   "/api/%2e%2e/secret", "/api/../secret", "/api/state#fragment", "/api\\state",
                   "/control/poll", "/api/bridge-deliver", "/agent/execute"]
        for target in targets:
            with self.subTest(target=target), self.assertRaises(SafeError): protocol.validate_route("GET", target)

    def test_unlisted_legacy_and_wrong_method_routes_are_not_published(self):
        routes = [("GET", "/api/kanban/source?path=private.txt"), ("GET", "/api/study-status"),
                  ("GET", "/api/future-internal-admin"), ("GET", "/api/action"),
                  ("POST", "/api/artifact-download/" + "a" * 43)]
        for method, target in routes:
            with self.subTest(method=method, target=target), self.assertRaises(SafeError):
                protocol.validate_route(method, target)

    def test_unexpected_and_duplicate_query_arguments_are_rejected(self):
        for method, target in (("POST", "/api/state?url=http://169.254.169.254/"),
                               ("POST", "/api/models?ignored=1"),
                               ("GET", "/api/decisions?thread=a&thread=b")):
            with self.subTest(target=target), self.assertRaises(SafeError): protocol.validate_route(method, target)

    def test_redirect_handler_never_follows_an_upstream(self):
        with self.assertRaises(SafeError):
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://127.0.0.1/private")


class RelayJobSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-security-")
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.telegram_calls = []
        def telegram(*args, **kwargs):
            self.telegram_calls.append((args, kwargs))
            return {"message_id": 1}
        self.relay = Relay(KEY, TOKEN, OWNER, ORIGIN, self.temp.name, clock=self.clock, telegram=telegram)
        self.headers = {"content-type": "application/json", "x-telegram-init-data": init_data()}
        self.lease = self.join("1" * 32)
        self.threads = []
        self.addCleanup(self.stop_waiters)
        self.poll_empty()

    def join(self, host_id):
        return self.relay.control("join", {"owner_id": OWNER, "public_url": ORIGIN,
            "bot_binding": protocol.binding(TOKEN), "platform": "macos", "host_id": host_id,
            "epoch": self.relay.epoch})

    def poll_empty(self):
        # Advance only synthetic time: a long-poll timeout must not delay tests.
        with patch.object(self.relay.condition, "wait", side_effect=lambda seconds: self.clock.advance(seconds)):
            return self.relay.control("poll", dict(self.lease, backend_ready=True))["job"]

    def stop_waiters(self):
        self.clock.advance(1000)
        with self.relay.condition: self.relay.condition.notify_all()
        for thread in self.threads: thread.join(2)

    def begin(self, target="/api/action", *, method="POST", body=b'{"request_id":"synthetic_request","action":"stop","payload":{}}', headers=None):
        outcome = {}
        def forward():
            try: outcome["value"] = self.relay.forward(method, target, self.headers if headers is None else headers, body)
            except Exception as error: outcome["error"] = error
        worker = threading.Thread(target=forward, daemon=True)
        self.threads.append(worker)
        worker.start()
        end = time.monotonic() + 2
        while not outcome and time.monotonic() < end:
            with self.relay.lock:
                if self.relay.jobs: return worker, outcome
            time.sleep(.001)
        self.fail("Synthetic request was not queued: " + repr(outcome))

    def claim(self):
        return self.relay.control("poll", dict(self.lease, backend_ready=True))["job"]

    def complete(self, job, body=b"{}", *, status=200, headers=None, lease=None):
        return self.relay.control("complete", dict(self.lease if lease is None else lease, id=job["id"],
            response={"status": status, "headers": headers or {"content-type": "application/json"},
                      "body": protocol.pack_body(body)}))

    def roundtrip(self, target, response, *, method="POST", body=b"{}", headers=None):
        worker, outcome = self.begin(target, method=method, body=body, headers=headers)
        job = self.claim()
        self.complete(job, response)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", outcome)
        return job, outcome["value"]

    def test_wrong_owner_forged_and_expired_auth_never_enqueue(self):
        for value in ("forged", init_data(OWNER + 1), init_data(age=7200), init_data(token="wrong-token")):
            with self.subTest(value_kind=value[:10]), self.assertRaises(SafeError):
                self.relay.forward("POST", "/api/action", {"x-telegram-init-data": value}, b"{}")
            self.assertEqual(self.relay.jobs, {})
            self.assertEqual(self.relay.pending_bytes, 0)

    def test_one_active_host_and_mismatched_bot_are_rejected(self):
        with self.assertRaises(SafeError): self.join("2" * 32)
        for field, value in (("owner_id", OWNER + 1), ("bot_binding", protocol.binding("different-token")),
                             ("public_url", "https://different.example.org")):
            payload = {"owner_id": OWNER, "public_url": ORIGIN, "bot_binding": protocol.binding(TOKEN),
                       "platform": "macos", "host_id": "1" * 32, "epoch": self.relay.epoch}
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(SafeError): self.relay.control("join", payload)

    def test_original_request_id_and_bytes_survive_relay_and_completion_is_once(self):
        original = b'{"request_id":"owner_action_stable_123","action":"send","payload":{"text":"same"}}'
        binary = bytes(range(256)) + "Синтетический отчёт".encode()
        job, result = self.roundtrip("/api/action", binary, body=original)
        self.assertEqual(protocol.unpack_body(job["body"]), original)
        self.assertEqual(result["body"], binary)
        with self.assertRaises(SafeError): self.complete(job, binary)
        self.assertIsNone(self.poll_empty())

    def test_expired_claim_is_not_requeued_and_late_result_is_rejected(self):
        worker, outcome = self.begin()
        job = self.claim()
        self.clock.advance(31)
        with self.relay.condition: self.relay.condition.notify_all()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcome.get("error"), SafeError)
        with self.assertRaises(SafeError): self.complete(job)
        self.assertIsNone(self.poll_empty())

    def test_new_lease_invalidates_old_completion_and_pending_mutation(self):
        worker, outcome = self.begin()
        job, old = self.claim(), dict(self.lease)
        self.clock.advance(self.relay.LEASE_SECONDS + 1)
        self.lease = self.join("2" * 32)
        with self.assertRaises(SafeError): self.complete(job, lease=old)
        worker.join(2)
        self.assertIsInstance(outcome.get("error"), SafeError)
        self.assertIsNone(self.poll_empty())

    def test_relay_restart_rejects_old_epoch_before_telegram_dispatch(self):
        restarted = Relay(KEY, TOKEN, OWNER, ORIGIN, self.temp.name, telegram=self.relay.telegram)
        current = restarted.control("join", {"owner_id": OWNER, "public_url": ORIGIN,
            "bot_binding": protocol.binding(TOKEN), "platform": "macos", "host_id": "1" * 32,
            "epoch": restarted.epoch})
        self.assertNotEqual(current["epoch"], self.lease["epoch"])
        with self.assertRaises(SafeError):
            restarted.control("telegram", dict(self.lease, operation_id="b" * 32,
                method="sendMessage", payload={"chat_id": OWNER, "text": "synthetic"}))
        self.assertEqual(self.telegram_calls, [])

    def test_captured_signed_join_cannot_acquire_lease_after_restart(self):
        channel = "request:/join"
        captured = protocol.signed(KEY, channel, {"owner_id": OWNER, "public_url": ORIGIN,
            "bot_binding": protocol.binding(TOKEN), "platform": "macos", "host_id": "1" * 32,
            "epoch": self.relay.epoch})
        restarted = Relay(KEY, TOKEN, OWNER, ORIGIN, self.temp.name, telegram=self.relay.telegram)
        # A fresh nonce cache still accepts a recently captured authentic envelope.
        # The server incarnation must independently reject its stale join payload.
        value = restarted.verifier.verify(channel, captured)
        with self.assertRaises(SafeError): restarted.control("join", value)
        self.assertIsNone(restarted.host)
        self.assertEqual(restarted.jobs, {})

    def test_unknown_telegram_mutation_is_not_resent_after_restart(self):
        def lost_reply(*args, **kwargs):
            self.telegram_calls.append((args, kwargs))
            raise TimeoutError("synthetic connection lost after delivery")
        self.relay.telegram = lost_reply
        operation = {"operation_id": "c" * 32, "method": "sendMessage",
                     "payload": {"chat_id": OWNER, "text": "synthetic"}}
        with self.assertRaises(SafeError): self.relay.control("telegram", dict(self.lease, **operation))
        restarted = Relay(KEY, TOKEN, OWNER, ORIGIN, self.temp.name, telegram=lost_reply)
        new = restarted.control("join", {"owner_id": OWNER, "public_url": ORIGIN,
            "bot_binding": protocol.binding(TOKEN), "platform": "macos", "host_id": "2" * 32,
            "epoch": restarted.epoch})
        with self.assertRaises(SafeError): restarted.control("telegram", dict(new, **operation))
        self.assertEqual(len(self.telegram_calls), 1)

    def test_telegram_recipient_and_method_are_restricted(self):
        for method, payload in (("sendMessage", {"chat_id": OWNER + 1, "text": "synthetic"}),
                                ("deleteWebhook", {}), ("setMyCommands", {"scope": {"type": "default"}})):
            with self.subTest(method=method), self.assertRaises(SafeError):
                self.relay.control("telegram", dict(self.lease, operation_id="d" * 32, method=method, payload=payload))
        self.assertEqual(self.telegram_calls, [])

    def test_aggregate_queue_limit_rejects_before_enqueue(self):
        with patch("codex_telegram.relay_server.MAX_PENDING_BYTES", 200):
            with self.assertRaises(SafeError): self.relay.forward("POST", "/api/action", self.headers, b"x" * 250)
        self.assertEqual(self.relay.jobs, {})
        self.assertEqual(self.relay.pending_bytes, 0)

    def ticket(self, token="a" * 43, *, expires=None, route=None):
        target = "/api/artifact-download/" + token
        response = protocol.json_bytes({"url": target, "name": "synthetic.txt",
                                       "expiresAt": int(time.time()) + 120 if expires is None else expires})
        self.roundtrip(route or "/api/threads/synthetic/artifacts/" + "a" * 32 + "/download-ticket", response)
        return target

    def test_download_capability_only_comes_from_authenticated_ticket_response(self):
        target = "/api/artifact-download/" + "a" * 43
        with self.assertRaises(SafeError): self.relay.authorize_public("GET", target, {})
        target = self.ticket()
        self.relay.authorize_public("HEAD", target, {})
        self.relay.authorize_public("GET", target, {})
        with self.assertRaises(SafeError): self.relay.authorize_public("GET", target[:-1] + "b", {})
        with self.assertRaises(SafeError): self.relay.authorize_public("POST", target, {})

    def test_unrelated_or_expired_response_cannot_issue_download_capability(self):
        for options in ({"route": "/api/state"}, {"expires": int(time.time()) - 1}):
            with self.subTest(options=options):
                target = self.ticket(**options)
                with self.assertRaises(SafeError): self.relay.authorize_public("GET", target, {})

    def test_new_host_lease_retires_download_capabilities(self):
        target = self.ticket()
        self.clock.advance(self.relay.LEASE_SECONDS + 1)
        self.lease = self.join("2" * 32)
        with self.assertRaises(SafeError): self.relay.authorize_public("GET", target, {})

    def test_download_capability_never_survives_more_than_ten_minutes(self):
        issued = time.time()
        target = self.ticket(expires=int(issued) + 3500)
        with patch("codex_telegram.relay_server.time.time", return_value=issued + 601):
            with self.assertRaises(SafeError): self.relay.authorize_public("GET", target, {})

    def test_owner_authorization_does_not_override_disconnected_backend(self):
        self.relay.control("poll", dict(self.lease, backend_ready=False))
        with self.assertRaises(SafeError): self.relay.forward("POST", "/api/action", self.headers, b"{}")
        self.assertEqual(self.relay.jobs, {})


class RelayUpstreamSecurityTests(unittest.TestCase):
    def test_download_path_cannot_escape_fixed_telegram_origin(self):
        opener = Mock()
        with patch("codex_telegram.relay_server.urllib.request.build_opener", return_value=opener):
            for path in ("https://evil.example/file", "//evil.example/file", "../auth", "/file", "a/../file", "a%2Ffile"):
                with self.subTest(path=path), self.assertRaises(SafeError):
                    telegram_http(TOKEN, "getFile", {}, file_path=path)
        opener.open.assert_not_called()

    def test_upstream_oversize_response_is_rejected_not_truncated(self):
        response = Mock()
        response.read.return_value = b"x" * 9
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = context
        with patch("codex_telegram.relay_server.urllib.request.build_opener", return_value=opener), \
                patch("codex_telegram.relay_server.MAX_BODY", 8):
            with self.assertRaises(SafeError): telegram_http(TOKEN, "getFile", {}, file_path="documents/synthetic.txt")
        response.read.assert_called_once_with(9)
        self.assertTrue(opener.open.call_args.args[0].full_url.startswith("https://api.telegram.org/file/bot"))

    def test_native_forward_is_fixed_loopback_and_does_not_follow_redirects(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 302
        response.getheaders.return_value = [("Location", "http://169.254.169.254/metadata"),
                                             ("Set-Cookie", "private=value")]
        response.read.return_value = b"redirect"
        with patch.object(host_connector.http.client, "HTTPConnection", return_value=connection) as connect:
            result = host_connector.local_request("POST", "/api/state", {"Host": "evil.example",
                        "Authorization": "private", "X-Telegram-Init-Data": "synthetic"}, b"{}", ORIGIN)
        connect.assert_called_once_with("127.0.0.1", 8787, timeout=25)
        connection.request.assert_called_once()
        sent = connection.request.call_args.kwargs["headers"]
        self.assertEqual(sent["Host"], "panel.example.org")
        self.assertNotIn("Authorization", sent)
        self.assertEqual(sent["x-telegram-init-data"], "synthetic")
        self.assertEqual(result["status"], 302)
        self.assertNotIn("location", result["headers"])
        self.assertNotIn("set-cookie", result["headers"])
        connection.close.assert_called_once()

    def test_native_uncertain_mutation_is_never_retried_by_worker(self):
        client = Mock()
        client.public_url = ORIGIN
        client.stopped = threading.Event()
        original = b'{"request_id":"synthetic_stable_id","action":"stop"}'
        job = {"id": "a" * 32, "method": "POST", "path": "/api/action",
               "headers": {"x-telegram-init-data": "synthetic"}, "body": protocol.pack_body(original)}
        client.request.side_effect = [{"job": job}, SafeError("synthetic lost completion")]
        forward = Mock(side_effect=TimeoutError("synthetic result lost after execution"))
        worker = host_connector.HostConnector(client, forward=forward, ready=lambda: True)
        worker._run()
        self.assertTrue(client.stopped.is_set())
        forward.assert_called_once_with("POST", "/api/action", job["headers"], original, ORIGIN)
        self.assertEqual([call.args[0] for call in client.request.call_args_list], ["poll", "complete"])


class RelayHttpFramingSecurityTests(unittest.TestCase):
    def request(self, method, target, headers):
        relay = Mock()
        relay.public_url = ORIGIN
        handler_type = handler(relay)
        request = handler_type.__new__(handler_type)
        request.command, request.path = method, target
        request.headers = HTTPMessage()
        for key, value in [("Host", "panel.example.org"), *headers]:
            request.headers[key] = value
        request.rfile = Mock()
        request.rfile.read.return_value = b"x"
        request.reply = Mock()
        return relay, request

    def test_anonymous_asset_request_cannot_allocate_a_large_body(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                relay, request = self.request(method, "/", [("Content-Length", "21000000")])
                request.dispatch()
                request.rfile.read.assert_not_called()
                relay.forward.assert_not_called()
                self.assertGreaterEqual(request.reply.call_args.args[0], 400)

    def test_duplicate_content_lengths_and_transfer_encoding_are_rejected_before_read(self):
        cases = [[("Content-Length", "1"), ("Content-Length", "2")],
                 [("Content-Length", "1"), ("Content-Length", "1")],
                 [("Content-Length", "1"), ("Transfer-Encoding", "chunked")]]
        for headers in cases:
            with self.subTest(headers=headers):
                _, request = self.request("POST", "/api/action", headers)
                with self.assertRaises(SafeError): request.body(protocol.MAX_BODY)
                request.rfile.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()

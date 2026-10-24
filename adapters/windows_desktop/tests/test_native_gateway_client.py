"""Artificial IPC and discovery tests; no real engine or Telegram calls."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from native_gateway_client import GatewayError, MAX_FRAME, NativeGatewayClient, SCHEMA

THREAD = "22db506f-36bc-5200-af96-087ee32092c6"
OTHER = "fe35f79b-f08b-54de-a91d-e2897d04b982"


class FakeConnection:
    def __init__(self, reply=None, *, poll=True, write_error=False):
        self.reply = reply or {"ok": True, "result": {"status": "ready"}}
        self.poll_result = poll
        self.write_error = write_error
        self.sent = []
        self.closed = False

    def send_bytes(self, payload):
        self.sent.append(json.loads(payload))
        if self.write_error:
            raise OSError("private diagnostic")

    def poll(self, timeout):
        return self.poll_result

    def recv_bytes(self, maximum):
        assert maximum == MAX_FRAME
        return json.dumps(self.reply).encode()

    def close(self):
        self.closed = True


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "native_gateway"
        self.keys = Path(self.temp.name) / "native_gateway_keys"
        self.directory.mkdir()
        self.keys.mkdir()
        self.connection = FakeConnection()
        self.connect_calls = []
        self.manifest()

    def manifest(self, epoch="one", **overrides):
        key = self.keys / f"{epoch}.bin"
        key.write_bytes(b"encrypted-key")
        value = {"schema": SCHEMA, "epoch": epoch, "pid": 222,
                 "pipe": "\\\\.\\pipe\\codex-native-gateway-" + epoch,
                 "thread_ids": [THREAD], "authkey_file": str(key.resolve())}
        value.update(overrides)
        (self.directory / f"{epoch}.json").write_text(json.dumps(value))

    def connect(self, pipe, **kwargs):
        self.connect_calls.append((pipe, kwargs))
        return self.connection

    def client(self, **kwargs):
        return NativeGatewayClient(directory=self.directory, keys_directory=self.keys,
                                   decrypt_key=lambda value: b"k" * 32,
                                   process_alive=lambda pid: True,
                                   connect=kwargs.pop("connect", self.connect),
                                   timeout=kwargs.pop("timeout", .2), **kwargs)

    def error(self, fn, code, uncertain=False):
        with self.assertRaises(GatewayError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.uncertain, uncertain)
        self.assertNotIn("private", str(raised.exception))

    def test_routes_exact_thread_and_authenticates(self):
        result = self.client().status(thread_id=THREAD)
        self.assertEqual(result, {"status": "ready"})
        self.assertEqual(self.connection.sent, [{"version": 1, "command": "status", "thread_id": THREAD}])
        self.assertEqual(self.connect_calls[0][1], {"family": "AF_PIPE", "authkey": b"k" * 32})
        self.assertTrue(self.connection.closed)

    def test_unknown_thread_never_connects(self):
        self.error(lambda: self.client().status(thread_id=OTHER), "native_gateway_unavailable")
        self.assertFalse(self.connect_calls)

    def test_ambiguous_manifest_is_not_arbitrarily_selected(self):
        self.manifest("two")
        self.error(lambda: self.client().status(thread_id=THREAD), "native_gateway_ambiguous")
        self.assertFalse(self.connect_calls)

    def test_multiple_engines_route_only_their_known_thread(self):
        self.manifest("two", thread_ids=[OTHER])
        self.assertEqual(self.client().discover(OTHER).epoch, "two")

    def test_dead_process_excluded(self):
        self.error(lambda: NativeGatewayClient(directory=self.directory, process_alive=lambda pid: False).discover(),
                   "native_gateway_unavailable")

    def test_malicious_pipe_or_key_path_is_rejected(self):
        for overrides in [{"pipe": "\\\\remote\\pipe\\secret"},
                          {"authkey_file": str(Path(self.temp.name) / "outside.bin")},
                          {"epoch": "../one"}, {"pid": True}, {"thread_ids": ["not-a-uuid"]}]:
            self.manifest(**overrides)
            self.assertEqual(self.client().instances(), [])

    def test_answer_preserves_exact_response_and_operation(self):
        response = {"answers": {"q": {"answers": ["Да — мой вариант\nстрока 2"]}}}
        self.connection.reply = {"ok": True, "operation_id": "op-1", "result": {"status": "accepted"}}
        self.assertEqual(self.client().answer(thread_id=THREAD, request_key="request:2", response=response,
                                             operation_id="op-1"), {"status": "accepted"})
        self.assertEqual(self.connection.sent[0]["response"], response)
        self.assertEqual(self.connection.sent[0]["operation_id"], "op-1")
        self.assertEqual(len(self.connect_calls), 1)

    def test_owner_input_requires_exact_echo(self):
        self.connection.reply = {"ok": True, "operation_id": "other", "result": {"status": "accepted"}}
        self.error(lambda: self.client().owner_input(thread_id=THREAD, prompt="Контроль", operation_id="op-1"),
                   "native_gateway_correlation_invalid", True)
        self.assertEqual(len(self.connection.sent), 1)

    def test_timeout_after_send_is_uncertain_and_not_retried(self):
        self.connection.poll_result = False
        self.error(lambda: self.client().owner_input(thread_id=THREAD, prompt="Контроль", operation_id="op-1"),
                   "native_gateway_timeout", True)
        self.assertEqual(len(self.connect_calls), 1)
        self.assertEqual(len(self.connection.sent), 1)

    def test_failed_write_is_uncertain_and_not_retried(self):
        self.connection.write_error = True
        self.error(lambda: self.client().owner_input(thread_id=THREAD, prompt="Контроль", operation_id="op-1"),
                   "native_gateway_transport_failed", True)
        self.assertEqual(len(self.connect_calls), 1)

    def test_connection_failure_before_write_is_definite(self):
        def fail(*args, **kwargs):
            raise OSError("private error")
        self.error(lambda: self.client(connect=fail).owner_input(thread_id=THREAD, prompt="Контроль", operation_id="op-1"),
                   "native_gateway_transport_failed")

    def test_timed_out_connect_never_sends_later(self):
        release = threading.Event()
        finished = threading.Event()
        def slow(*args, **kwargs):
            release.wait(2)
            finished.set()
            return self.connection
        started = time.monotonic()
        self.error(lambda: self.client(connect=slow, timeout=.02).owner_input(
            thread_id=THREAD, prompt="Контроль", operation_id="op-1"), "native_gateway_timeout")
        self.assertLess(time.monotonic() - started, .3)
        release.set()
        self.assertTrue(finished.wait(1))
        for _ in range(20):
            if self.connection.closed:
                break
            time.sleep(.002)
        self.assertFalse(self.connection.sent)
        self.assertTrue(self.connection.closed)

    def test_oversized_request_is_rejected_before_connect(self):
        self.error(lambda: self.client().owner_input(thread_id=THREAD, prompt="x" * MAX_FRAME, operation_id="op-1"),
                   "native_gateway_request_too_large")
        self.assertFalse(self.connect_calls)

    def test_explicit_server_rejection_is_definite(self):
        self.connection.reply = {"ok": False, "operation_id": "op-1", "error": "request_expired", "uncertain": False}
        self.error(lambda: self.client().answer(thread_id=THREAD, request_key="request:2", response={}, operation_id="op-1"),
                   "request_expired")

    def test_server_internal_error_is_uncertain(self):
        self.connection.reply = {"ok": False, "operation_id": "op-1", "error": "engine_timeout"}
        self.error(lambda: self.client().answer(thread_id=THREAD, request_key="request:2", response={}, operation_id="op-1"),
                   "engine_timeout", True)

    def test_invalid_command_and_missing_operation_never_connect(self):
        self.error(lambda: self.client().call("approve_all"), "native_gateway_command_invalid")
        self.error(lambda: self.client().owner_input(thread_id=THREAD, prompt="Контроль", operation_id=""),
                   "native_gateway_operation_invalid")
        self.assertFalse(self.connect_calls)

    def test_readonly_listing_does_not_need_operation(self):
        self.client().list_requests(thread_id=THREAD)
        self.assertEqual(self.connection.sent[0]["command"], "list_requests")


if __name__ == "__main__":
    unittest.main()

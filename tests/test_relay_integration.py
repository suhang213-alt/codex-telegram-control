"""Synthetic Docker transport, using only isolated loopback HTTP fixtures."""
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_telegram.config import SafeError
from codex_telegram import relay_client, host_connector
from codex_telegram.relay_server import Relay, BoundedServer, handler
from codex_telegram.relay_protocol import pack_body, unpack_body
from codex_telegram.telegram import Telegram

TOKEN = "123456:" + "x" * 30
OWNER = 424242
ORIGIN = "https://panel.example.test"


def init_data():
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": OWNER})}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class RelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tele_calls = []
        def telegram(token, method, payload, **extra):
            self.tele_calls.append((method, payload, extra))
            if extra.get("file_path") is not None: return {"body": pack_body(b"synthetic attachment")}
            if method == "getUpdates":
                return [{"update_id": 10, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "text": "owned"}},
                        {"update_id": 11, "message": {"from": {"id": 88}, "chat": {"id": 88, "type": "private"}, "text": "other user's content"}}]
            return {"message_id": 1}
        self.relay = Relay(b"k" * 32, TOKEN, OWNER, ORIGIN, self.temp.name, telegram=telegram)
        self.control = self.serve(BoundedServer(("127.0.0.1", 0), handler(self.relay, control=True)))
        self.public = self.serve(BoundedServer(("127.0.0.1", 0), handler(self.relay)))
        self.url_patch = patch.object(relay_client, "CONTROL_URL", f"http://127.0.0.1:{self.control.server_port}")
        self.url_patch.start(); self.addCleanup(self.url_patch.stop)
        self.client = relay_client.RelayClient(b"k" * 32, TOKEN, OWNER, ORIGIN, "macos")
        self.client.join()
        self.addCleanup(self.client.stopped.set)

    def serve(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def public_request(self, method, path, body=b"", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.public.server_port, timeout=5)
        try:
            values = {"Host": "panel.example.test", "Content-Type": "application/json", **(headers or {})}
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally: connection.close()

    def test_public_route_preserves_auth_operation_id_and_body_through_host_handler(self):
        received = []
        class LocalHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, self.headers["X-Telegram-Init-Data"], self.headers["Host"], body))
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "11"); self.end_headers(); self.wfile.write(b'{"ok":true}')
        local = self.serve(ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler))
        original = http.client.HTTPConnection
        def isolated_connection(host, port=None, **kwargs):
            self.assertEqual((host, port), ("127.0.0.1", 8787))
            return original(host, local.server_port, **kwargs)
        def forward(*args):
            return host_connector.local_request(*args, _connection_factory=isolated_connection)
        worker = host_connector.HostConnector(self.client, ready=lambda: True, forward=forward)
        self.addCleanup(worker.stop)
        with patch("subprocess.Popen", side_effect=AssertionError("No process launch")):
            worker.start()
            deadline = time.monotonic() + 3
            while not self.relay.connected() and time.monotonic() < deadline: time.sleep(.01)
            auth = init_data()
            body = b'{"request_id":"same-operation-id","action":"send","payload":{"text":"synthetic"}}'
            status, _, answer = self.public_request("POST", "/api/action", body, {"X-Telegram-Init-Data": auth})
            self.assertEqual((status, answer), (200, b'{"ok":true}'))
            self.assertEqual(received, [("/api/action", auth, "panel.example.test", body)])
            bad, _, _ = self.public_request("POST", "/api/action", body)
            self.assertNotEqual(bad, 200)
            self.assertEqual(len(received), 1)

    def test_control_is_not_public_and_health_is_only_liveness(self):
        status, _, body = self.public_request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["host_connected"])
        status, _, _ = self.public_request("POST", "/telegram", b"{}")
        self.assertNotEqual(status, 200)
        self.assertEqual(self.tele_calls, [])

    def test_telegram_transport_multipart_download_and_owner_filter(self):
        result = self.client.telegram("getUpdates", {"offset": 0})
        self.assertEqual(result[0]["message"]["text"], "owned")
        self.assertEqual(result[1], {"update_id": 11})
        self.client.document("report.txt", b"synthetic report")
        self.assertEqual(unpack_body(self.tele_calls[-1][2]["document"]["body"]), b"synthetic report")
        self.assertEqual(self.client.download("documents/synthetic.txt"), b"synthetic attachment")

    def test_mac_transport_hooks_have_no_direct_network_fallback(self):
        fake = Mock()
        transport = Telegram(TOKEN, OWNER, ORIGIN, Mock())
        with patch.dict(os.environ, {"CTD_RELAY_KEY_FILE": "synthetic"}), \
             patch.object(relay_client, "current", return_value=fake), \
             patch("urllib.request.urlopen", side_effect=AssertionError("Direct Telegram forbidden")) as network:
            transport.call("getMe", {})
            transport.send_document("report.txt", b"fixture")
            fake.telegram.assert_called_once_with("getMe", {})
            fake.document.assert_called_once_with("report.txt", b"fixture")
            network.assert_not_called()
        with patch.dict(os.environ, {"CTD_RELAY_KEY_FILE": "synthetic"}), \
             patch.object(relay_client, "current", side_effect=SafeError("offline")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("Direct Telegram forbidden")):
            with self.assertRaises(SafeError): transport.call("getMe", {})

    def test_failed_completion_stops_worker_without_replaying_local_mutation(self):
        class Client:
            public_url = ORIGIN
            stopped = threading.Event()
            def __init__(self): self.calls = []
            def request(self, action, payload):
                self.calls.append(action)
                if action == "poll": return {"job": {"id": "synthetic", "method": "POST", "path": "/api/action", "headers": {}, "body": pack_body(b"{}")}}
                raise SafeError("completion was lost")
        client = Client(); client.stopped.clear()
        forward = Mock(return_value={"status": 200, "body": pack_body(b"{}"), "headers": {}})
        worker = host_connector.HostConnector(client, forward=forward, ready=lambda: True)
        worker._run()
        self.assertTrue(client.stopped.is_set())
        self.assertEqual(client.calls, ["poll", "complete"])
        forward.assert_called_once()

    def test_mac_cannot_activate_connector_when_its_local_bind_failed(self):
        from codex_telegram import desktop_controller, cli, server
        config = {"port": 8787, "owner_id": OWNER, "public_url": ORIGIN}
        with patch.dict(os.environ, {"CTD_RELAY_KEY_FILE": "synthetic"}), \
                patch.object(desktop_controller, "token", return_value=TOKEN), \
                patch.object(cli, "InstanceLock"), patch.object(desktop_controller, "DesktopController"), \
                patch.object(server, "Server", side_effect=OSError("port already held")), \
                patch.object(relay_client, "start") as start, patch.object(relay_client, "close"):
            with self.assertRaises(OSError): desktop_controller.run(config, Path(self.temp.name))
            start.assert_not_called()


if __name__ == "__main__": unittest.main()

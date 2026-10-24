"""Real Docker port-mapping smoke with an artificial native backend only.

No Desktop, Telegram, account profile, tunnel, or model is contacted. The test
requires free loopback ports 8787/8788 and refuses an existing local listener.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from codex_telegram.config import SafeError
from codex_telegram.host_connector import HostConnector
from codex_telegram.relay_client import RelayClient
from codex_telegram.security import authenticate

spec = importlib.util.spec_from_file_location("docker_setup", ROOT / "scripts" / "docker.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

OWNER = 123456789
TOKEN = str(OWNER) + ":" + "A" * 35
PUBLIC = "https://panel.example.org"


def docker(args, *, timeout=30):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Docker roundtrip command failed: " + args[0])
    return result.stdout.strip()


def init_data(owner=OWNER):
    data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": owner}, separators=(",", ":"))}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, "\n".join(f"{key}={data[key]}" for key in sorted(data)).encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def request(port, path, *, payload=None, authorized=True):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=38)
    headers = {"Host": "panel.example.org", "Origin": PUBLIC, "Content-Type": "application/json"}
    if authorized:
        headers["X-Telegram-Init-Data"] = init_data()
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
    try:
        connection.request("POST" if body is not None else "GET", path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read(65536))
    finally:
        connection.close()


def run(image):
    if os.name != "posix" or os.getuid() == 0 or os.getgid() == 0:
        raise RuntimeError("Run this smoke as an ordinary non-root Linux CI user.")
    calls, ledger, received = Counter(), {}, []
    state_lock = threading.Lock()

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = b"fixture backend"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                authenticate(self.headers.get("X-Telegram-Init-Data", ""), TOKEN, OWNER)
                if self.path != "/api/action" or self.headers.get("Host") != "panel.example.org":
                    raise SafeError("Unexpected fixture route")
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                identifier = value["request_id"]
                with state_lock:
                    calls[identifier] += 1
                    received.append(value)
                    ledger.setdefault(identifier, {"accepted": True, "request_id": identifier})
                    response = ledger[identifier]
                if value.get("simulate_lost_response"):
                    # The synthetic backend committed, then lost its response.
                    # A correct connector must not send this command again.
                    self.close_connection = True
                    return
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (SafeError, ValueError, KeyError):
                self.send_error(401)

    # Bind our own backend before the container/worker can contact localhost.
    backend = ThreadingHTTPServer(("127.0.0.1", 8787), Backend)
    backend.daemon_threads = True
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 8788))
    except Exception:
        backend.server_close()
        raise
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    container = network = connector = temporary = receipt = None
    try:
        temporary = tempfile.TemporaryDirectory(prefix="docker-roundtrip-fixture-")
        temp = temporary.name
        base = Path(temp).resolve()
        state = base / "state"
        state.mkdir(mode=0o700)
        key = secrets.token_bytes(32)
        setup.write_new_private(base / "telegram_bot_token", TOKEN + "\n")
        setup.write_new_private(base / "connector_key", key.hex() + "\n")
        # Test the same bridge port publishing used in production. The first
        # CI engine did not expose the expected NAT mappings in internal mode.
        # The fixture blocks Python socket.connect/getaddrinfo; this is not
        # a complete OS network sandbox. No cloudflared container is started.
        network = docker(["network", "create", "ctc-fixture-" + secrets.token_hex(8)])
        if not re.fullmatch(r"[a-f0-9]{64}", network):
            raise RuntimeError("Unexpected Docker network identifier")
        args = ["run", "--detach", "--network", network, "--read-only", "--user", f"{os.getuid()}:{os.getgid()}",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true", "--pids-limit", "128",
                "--memory", "256m", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777",
                "--publish", "127.0.0.1:8788:8788", "--publish", "127.0.0.1::8787"]
        for name in ("telegram_bot_token", "connector_key"):
            args += ["--mount", f"type=bind,source={base / name},target=/run/secrets/{name},readonly"]
        guard = ("import sys\nfrom pathlib import Path\n"
                 "def deny_outbound(event,args):\n"
                 " if event in ('socket.connect','socket.getaddrinfo'):\n"
                 "  Path('/data/outbound-attempts').write_text('blocked')\n"
                 "  raise RuntimeError('Fixture outbound connection prohibited')\n"
                 "sys.addaudithook(deny_outbound)\n"
                 "from codex_telegram.relay_server import main\nraise SystemExit(main())\n")
        args += ["--mount", f"type=bind,source={state},target=/data", "--entrypoint", "python", image, "-B", "-c", guard,
                 "--public-url", PUBLIC, "--owner-id", str(OWNER), "--token-file", "/run/secrets/telegram_bot_token",
                 "--key-file", "/run/secrets/connector_key", "--state-dir", "/data"]
        container = docker(args)
        if not re.fullmatch(r"[a-f0-9]{64}", container):
            raise RuntimeError("Unexpected Docker container identifier")
        inspection = json.loads(docker(["inspect", container]))[0]
        ports = inspection["NetworkSettings"]["Ports"]
        print(json.dumps({"container_mapping_probe": ports, "running": inspection["State"]["Running"],
                          "exit_code": inspection["State"]["ExitCode"]}))
        assert ports["8788/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": "8788"}], "Missing loopback Docker control mapping"
        assert ports["8787/tcp"][0]["HostIp"] == "127.0.0.1"
        port = int(ports["8787/tcp"][0]["HostPort"])
        for attempt in range(30):
            try:
                if request(port, "/healthz")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(.2)
        else:
            raise RuntimeError("Container public listener did not start")
        assert request(port, "/healthz")[1]["host_connected"] is False
        assert request(port, "/api/action", payload={"request_id": "unauthorized"}, authorized=False)[0] != 200
        assert not received
        client = RelayClient(key, TOKEN, OWNER, PUBLIC, "macos")
        client.join()
        connector = HostConnector(client)
        connector.start()
        for attempt in range(30):
            if request(port, "/healthz")[1]["host_connected"]:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("Host connector did not acquire a live lease")
        original = {"request_id": "fixture_select_1", "action": "select", "payload": {"thread_id": "00000000-0000-4000-8000-000000000001"}}
        status, result = request(port, "/api/action", payload=original)
        assert status == 200 and result["request_id"] == original["request_id"]
        assert received == [original]
        # Explicitly repeating the same owner request preserves its ID;
        # the artificial native ledger remains the mutation authority.
        assert request(port, "/api/action", payload=original)[0] == 200
        assert len(ledger) == 1 and calls["fixture_select_1"] == 2
        uncertain = {"request_id": "fixture_unknown_1", "action": "send", "payload": {"text": "synthetic fixture"}, "simulate_lost_response": True}
        status, result = request(port, "/api/action", payload=uncertain)
        assert status == 503 and ("unconfirmed" in result.get("error", "").lower() or "uncertain" in result.get("error", "").lower())
        time.sleep(.8)
        assert calls["fixture_unknown_1"] == 1 and len(ledger) == 2
        assert not (state / "outbound-attempts").exists(), "Unexpected outbound attempt was blocked"
        receipt = {"schema": "container-host-roundtrip/1", "passed": True, "real_container": True,
                          "desktop_backend": "synthetic", "owner_auth_twice": True, "request_id_preserved": True,
                          "lost_response_not_replayed": True, "loopback_ports": True, "docker_network": "bridge",
                          "fixture_guard": "Python socket.connect and socket.getaddrinfo blocked",
                          "python_connect_or_getaddrinfo_attempts": 0,
                          "live_telegram": False, "live_desktop": False, "live_model": False}
    finally:
        cleanup = []
        if connector: cleanup.append(connector.stop)
        if container: cleanup.append(lambda: docker(["rm", "--force", container]))
        if network: cleanup.append(lambda: docker(["network", "rm", network]))
        cleanup.extend((backend.shutdown, backend.server_close))
        if temporary: cleanup.append(temporary.cleanup)
        failures = []
        for action in cleanup:
            try: action()
            except Exception as error: failures.append(type(error).__name__)
        if failures:
            raise RuntimeError("Docker fixture cleanup failed: " + ", ".join(failures))
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="codex-telegram-control:runtime-smoke")
    run(parser.parse_args().image)

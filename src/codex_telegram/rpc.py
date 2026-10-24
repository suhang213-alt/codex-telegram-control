"""Bidirectional stdio client. Does not expose an arbitrary RPC endpoint."""
from __future__ import annotations
import json
import os
import queue
import subprocess
import threading
from .config import SafeError
from . import __version__


class Rpc:
    def __init__(self, command, cwd, codex_home, on_event):
        self.on_event = on_event
        self.pending = {}
        self.lock = threading.RLock()
        self.counter = 0
        self.closed = False
        env = dict(os.environ)
        # Bot credentials and parent gateway overrides never reach the model process.
        for key in ("TELEGRAM_BOT_TOKEN", "CODEX_CLI_PATH", "CODEX_THREAD_ID", "OPENAI_API_KEY", "CODEX_API_KEY",
                    "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_MCP_NODE_PATH", "CTD_RELAY_KEY_FILE", "CTD_RELAY_PUBLIC_URL"):
            env.pop(key, None)
        env["CODEX_HOME"] = str(codex_home)
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "codex_telegram_control",
                        "title": "Telegram Control", "version": __version__},
                        "capabilities": {"experimentalApi": False}}, timeout=20)
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def send(self, message):
        body = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with self.lock:
            if self.closed or self.process.poll() is not None:
                raise SafeError("Codex connection closed. Restart the bridge and check status.")
            try:
                self.process.stdin.write(body)
                self.process.stdin.flush()
            except OSError:
                raise SafeError("Codex connection lost; outcome may be unknown. Check before retrying.") from None

    def call(self, method, params, timeout=30):
        with self.lock:
            self.counter += 1
            request_id = self.counter
            response = queue.Queue(maxsize=1)
            self.pending[request_id] = response
        try:
            self.send({"id": request_id, "method": method, "params": params})
            try:
                result = response.get(timeout=timeout)
            except queue.Empty:
                raise SafeError("Codex response timed out; outcome is unknown. Do not automatically resend.") from None
            if "error" in result:
                raise SafeError("Codex rejected the operation. Check login, permissions and CLI compatibility.")
            return result.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def reply(self, request_id, result=None, error=None):
        self.send({"id": request_id, **({"error": error} if error else {"result": result})})

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 8 * 1024 * 1024:
                    break
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        continue
                    with self.lock:
                        target = self.pending.get(value.get("id")) if "method" not in value else None
                    if target:
                        target.put_nowait(value)
                    elif "method" in value:
                        self.on_event(value)
                except (ValueError, TypeError, queue.Full):
                    continue
        finally:
            with self.lock:
                self.closed = True
                for target in self.pending.values():
                    try:
                        target.put_nowait({"error": {"code": "connection_closed"}})
                    except queue.Full:
                        pass
            self.on_event({"method": "bridge/disconnected", "params": {}})

    def close(self):
        with self.lock:
            self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()

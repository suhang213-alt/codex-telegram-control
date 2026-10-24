"""Native Desktop MCP client. No model turns, queue commands or app-server."""
from __future__ import annotations
import bridge_config as config

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from typing import Any
import uuid

PLUGIN_DIR = config.plugin_dir()
SERVICE_THREAD_ID = config.service_thread_id()
MAX_FRAME = 8 * 1024 * 1024
READ_ONLY_TOOLS = frozenset({
    "list_threads", "list_archived_threads", "list_projects", "read_thread",
    "wait_threads", "get_usage_limits", "list_artifacts", "get_handoff_status",
    "get_worktree_creation_status",
})
MUTATING_TOOLS = frozenset({
    "send_message_to_thread", "create_thread", "set_thread_title",
    "set_thread_archived", "set_thread_read_state", "move_thread_to_project",
    "move_thread_to_sidebar_section", "update_sidebar_preferences",
})
ALLOWED_TOOLS = READ_ONLY_TOOLS | MUTATING_TOOLS


class NativeError(RuntimeError):
    """Fixed diagnostic code; no raw exception text or private MCP responses."""
    def __init__(self, code: str, uncertain: bool = False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


class NativeMcpClient:
    def __init__(self, *, caller_thread_id: str | None = None,
                 request_timeout: float = 30, environment: dict | None = None):
        self._environment = dict(os.environ)
        if environment is not None:
            allowed = {"CODEX_THREAD_ID", "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_MCP_NODE_PATH"}
            if set(environment) - allowed:
                raise NativeError("unexpected_native_environment")
            self._environment.update(environment)
        inherited = self._environment.get("CODEX_THREAD_ID", "").strip()
        if inherited:
            self.thread_id = inherited
            if caller_thread_id is not None and caller_thread_id != inherited:
                raise NativeError("caller_context_mismatch")
        elif caller_thread_id == SERVICE_THREAD_ID:
            # This is routing context, not a credential. The inherited native
            # pipe and Desktop's own checks remain mandatory after restart.
            self.thread_id = caller_thread_id
        else:
            raise NativeError("caller_context_missing")
        try:
            uuid.UUID(self.thread_id)
        except (ValueError, TypeError):
            raise NativeError("caller_context_invalid") from None
        if not self._environment.get("CODEX_APP_TOOLS_PIPE_PATH", "").strip():
            raise NativeError("native_pipe_context_missing")
        self._node = self._environment.get("CODEX_MCP_NODE_PATH", "").strip()
        if not self._node or not Path(self._node).is_file():
            raise NativeError("native_node_unavailable")
        self._server = PLUGIN_DIR / "server.mjs"
        if not self._server.is_file():
            raise NativeError("native_plugin_unavailable")
        self.request_timeout = request_timeout
        self._process: subprocess.Popen | None = None
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._id = 0
        self._dead = False
        self._ready = False
        self._tools: dict[str, dict] = {}

    def start(self) -> list[str]:
        if self._ready:
            return sorted(self._tools)
        if self._process is not None or self._dead:
            raise NativeError("native_client_not_restartable")
        try:
            # Exactly the runtime selected first by the bundled launcher.
            # Environment is inherited or supplied by the separately verified
            # same-session installed-Desktop endpoint discovery.
            self._process = subprocess.Popen(
                [self._node, str(self._server)], cwd=str(PLUGIN_DIR),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
                env=self._environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            raise NativeError("native_launch_failed") from None
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self._request("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "telegram-desktop-native-bridge", "version": "1"},
            }, mutating=False)
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized",
                         "params": {}})
            catalog = self._request("tools/list", {}, mutating=False)
            tools = catalog.get("tools") if isinstance(catalog, dict) else None
            if not isinstance(tools, list):
                raise NativeError("native_catalog_invalid")
            self._tools = {tool["name"]: tool for tool in tools
                           if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
            self._ready = True
            return sorted(self._tools)
        except Exception:
            self.close()
            raise

    def call(self, name: str, args: dict[str, Any], *,
             owner_authorized: bool = False, timeout: float | None = None) -> dict:
        if name not in ALLOWED_TOOLS:
            raise NativeError("native_tool_not_allowed")
        mutating = name in MUTATING_TOOLS
        if mutating and owner_authorized is not True:
            raise NativeError("owner_authorization_required")
        if not self._ready:
            raise NativeError("native_client_not_started")
        if name not in self._tools:
            raise NativeError("native_tool_unavailable")
        if not isinstance(args, dict):
            raise NativeError("native_arguments_invalid")
        if timeout is None and name == "wait_threads":
            requested_ms = args.get("timeoutMs", 50000)
            if not isinstance(requested_ms, (int, float)) or not 0 <= requested_ms <= 50000:
                raise NativeError("native_wait_timeout_invalid")
            timeout = max(55, requested_ms / 1000 + 10)
        result = self._request("tools/call", {
            "name": name, "arguments": args,
            "_meta": {"openai/threadId": self.thread_id},
        }, mutating=mutating, timeout=timeout)
        if not isinstance(result, dict):
            raise NativeError("native_result_invalid", uncertain=mutating)
        # isError belongs to the tool contract. The caller decides whether its
        # domain error is definitive; transport failures never trigger retries.
        return result

    def _request(self, method: str, params: dict, *, mutating: bool,
                 timeout: float | None = None):
        wait = self.request_timeout if timeout is None else timeout
        if not isinstance(wait, (int, float)) or not 0 < wait <= 70:
            raise NativeError("native_request_timeout_invalid")
        with self._pending_lock:
            if self._dead:
                raise NativeError("native_disconnected")
            self._id += 1
            identifier = self._id
            response = queue.Queue(maxsize=1)
            self._pending[identifier] = response
        try:
            self._write({"jsonrpc": "2.0", "id": identifier,
                         "method": method, "params": params})
        except NativeError:
            with self._pending_lock:
                self._pending.pop(identifier, None)
            raise NativeError("native_write_failed", uncertain=mutating) from None
        try:
            item = response.get(timeout=wait)
        except queue.Empty:
            raise NativeError("native_timeout", uncertain=mutating) from None
        finally:
            with self._pending_lock:
                self._pending.pop(identifier, None)
        if isinstance(item, NativeError):
            raise NativeError(item.code, uncertain=mutating)
        if "error" in item:
            # Even an RPC error can follow an external side effect.
            raise NativeError("native_rpc_error", uncertain=mutating)
        if "result" not in item:
            raise NativeError("native_response_invalid", uncertain=mutating)
        return item["result"]

    def _write(self, value: dict):
        try:
            encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                       + "\n").encode("utf-8")
        except (ValueError, TypeError):
            raise NativeError("native_arguments_invalid") from None
        if len(encoded) > MAX_FRAME:
            raise NativeError("native_request_too_large")
        with self._write_lock:
            if self._process is None or self._process.stdin is None or self._dead:
                raise NativeError("native_disconnected")
            try:
                remaining = memoryview(encoded)
                while remaining:
                    written = self._process.stdin.write(remaining)
                    if not written:
                        raise OSError("write failed")
                    remaining = remaining[written:]
                self._process.stdin.flush()
            except OSError:
                raise NativeError("native_write_failed") from None

    def _fail_all(self, code: str):
        with self._pending_lock:
            self._dead = True
            for pending in self._pending.values():
                try:
                    pending.put_nowait(NativeError(code))
                except queue.Full:
                    pass

    def _read(self):
        try:
            while True:
                line = self._process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    self._fail_all("native_disconnected")
                    return
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    self._fail_all("native_frame_invalid")
                    return
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    self._fail_all("native_frame_invalid")
                    return
                if not isinstance(item, dict):
                    self._fail_all("native_frame_invalid")
                    return
                if "method" in item:
                    if "id" in item:
                        # Never auto-answer approvals, elicitation or sampling.
                        self._fail_all("native_server_request_requires_attention")
                        return
                    continue
                identifier = item.get("id")
                if not isinstance(identifier, int):
                    self._fail_all("native_response_invalid")
                    return
                with self._pending_lock:
                    pending = self._pending.get(identifier)
                    if pending is not None:
                        try:
                            pending.put_nowait(item)
                        except queue.Full:
                            pass
        except (OSError, ValueError):
            self._fail_all("native_disconnected")

    def close(self):
        self._ready = False
        self._fail_all("native_closed")
        process = self._process
        if process is None:
            return
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()  # Only our Node MCP child.
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdout, process.stdin):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()

"""Official connector calls through a private, ephemeral app-server process.

No model turns, saved chat, auth-file reads or approval responses. The caller
owns durable delivery/claim state and must retain it on uncertain sync outcomes.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from find_codex_cli import find_codex_cli

MAX_FRAME = 8 * 1024 * 1024
ALLOWED_TOOLS = frozenset({"telegram_codex.bridge_status", "telegram_codex.desktop_sync"})
_ALLOWED_RPC = frozenset({"initialize", "thread/start", "mcpServer/tool/call"})


class CloudMcpError(RuntimeError):
    def __init__(self, code, *, uncertain=False):
        self.code = code
        self.uncertain = bool(uncertain)
        super().__init__(code)


def parse_tool_result(value, *, mutating=False):
    """Decode only official MCP content/structuredContent object responses."""
    def invalid(code="cloud_mcp_result_invalid"):
        raise CloudMcpError(code, uncertain=mutating)
    if not isinstance(value, dict) or not isinstance(value.get("content"), list):
        invalid()
    if value.get("isError") is not None and type(value["isError"]) is not bool:
        invalid()
    if value.get("isError") is True:
        content = value["content"]
        if (len(content) == 1 and isinstance(content[0], dict)
                and content[0].get("type") == "text"
                and content[0].get("text") == "claim_conflict"):
            # Exact owner-queue conflict code confirmed by the connector;
            # never expose any other tool text or interpolate diagnostics.
            invalid("claim_conflict")
        invalid("cloud_mcp_tool_error")
    structured = value.get("structuredContent")
    if structured is not None:
        if not isinstance(structured, dict):
            invalid()
        return structured
    texts = [part.get("text") for part in value["content"]
             if isinstance(part, dict) and part.get("type") == "text"]
    if len(texts) != 1 or not isinstance(texts[0], str):
        invalid()
    try:
        result = json.loads(texts[0])
    except (ValueError, TypeError):
        invalid()
    if not isinstance(result, dict):
        invalid()
    return result


class CloudMcpClient:
    def __init__(self, *, request_timeout=30, executable_resolver=None, process_factory=None):
        if (type(request_timeout) not in (int, float)
                or not math.isfinite(request_timeout) or not 0 < request_timeout <= 60):
            raise CloudMcpError("cloud_mcp_timeout_invalid")
        self._timeout = request_timeout
        self._resolver = executable_resolver or find_codex_cli
        self._process_factory = process_factory or subprocess.Popen
        self._process = None
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._calls = threading.Lock()
        self._next_id = 0
        self._dead = False
        self._ready = False
        self._thread_id = None

    def start(self):
        with self._calls:
            if self._ready:
                return self
            if self._process is not None or self._dead:
                raise CloudMcpError("cloud_mcp_not_restartable")
            try:
                executable = self._resolver()
                self._process = self._process_factory(
                    [executable, "app-server", "--stdio"],
                    cwd=str(Path(__file__).resolve().parent),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                threading.Thread(target=self._read, args=(self._process.stdout,),
                                 daemon=True).start()
                initialized = self._request("initialize", {
                    "clientInfo": {"name": "telegram-cloud-background-bridge", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                }, mutating=False)
                if not isinstance(initialized, dict):
                    raise CloudMcpError("cloud_mcp_initialize_invalid")
                self._notify_initialized()
                result = self._request("thread/start", {"ephemeral": True}, mutating=False)
                thread = result.get("thread") if isinstance(result, dict) else None
                if (not isinstance(thread, dict) or thread.get("ephemeral") is not True
                        or "path" not in thread or thread["path"] is not None
                        or thread.get("turns") != []):
                    raise CloudMcpError("cloud_mcp_ephemeral_context_unconfirmed")
                identifier = thread.get("id")
                if not isinstance(identifier, str):
                    raise CloudMcpError("cloud_mcp_ephemeral_context_unconfirmed")
                try:
                    uuid.UUID(identifier)
                except ValueError:
                    raise CloudMcpError("cloud_mcp_ephemeral_context_unconfirmed") from None
                self._thread_id = identifier
                self._ready = True
                return self
            except CloudMcpError:
                self.close()
                raise
            except Exception:
                self.close()
                raise CloudMcpError("cloud_mcp_start_failed") from None

    def call(self, tool, arguments, *, owner_authorized=False):
        if tool not in ALLOWED_TOOLS:
            raise CloudMcpError("cloud_mcp_tool_not_allowed")
        mutating = tool == "telegram_codex.desktop_sync"
        if mutating and owner_authorized is not True:
            raise CloudMcpError("cloud_mcp_owner_authorization_required")
        if not isinstance(arguments, dict):
            raise CloudMcpError("cloud_mcp_arguments_invalid")
        with self._calls:
            if not self._ready or self._dead:
                raise CloudMcpError("cloud_mcp_not_started")
            try:
                result = self._request("mcpServer/tool/call", {
                    "threadId": self._thread_id, "server": "codex_apps",
                    "tool": tool, "arguments": arguments,
                }, mutating=mutating)
                return parse_tool_result(result, mutating=mutating)
            except CloudMcpError:
                # A late result can never be mistaken for a later sync call.
                self.close()
                raise

    def bridge_status(self):
        return self.call("telegram_codex.bridge_status", {})

    def desktop_sync(self, arguments, *, owner_authorized=False):
        return self.call("telegram_codex.desktop_sync", arguments,
                         owner_authorized=owner_authorized)

    def _encode(self, value):
        try:
            encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise CloudMcpError("cloud_mcp_arguments_invalid") from None
        if len(encoded) > MAX_FRAME:
            raise CloudMcpError("cloud_mcp_request_too_large")
        return encoded

    def _write_frame(self, encoded, complete=None):
        try:
            with self._write_lock:
                process = self._process
                if process is None or self._dead or process.stdin is None:
                    raise OSError()
                remaining = memoryview(encoded)
                while remaining:
                    written = process.stdin.write(remaining)
                    if not written:
                        raise OSError()
                    remaining = remaining[written:]
                process.stdin.flush()
        except Exception:
            self._fail_all("cloud_mcp_write_failed")
        finally:
            if complete is not None:
                complete.set()

    def _notify_initialized(self):
        # Write completion is bounded too; a stalled pipe is terminated by
        # start's error cleanup instead of hanging the background coordinator.
        completed = threading.Event()
        threading.Thread(target=self._write_frame,
                         args=(self._encode({"method": "initialized"}), completed),
                         daemon=True).start()
        if not completed.wait(self._timeout) or self._dead:
            raise CloudMcpError("cloud_mcp_initialize_notification_failed")

    def _request(self, method, params, *, mutating):
        if method not in _ALLOWED_RPC:
            raise CloudMcpError("cloud_mcp_method_not_allowed")
        with self._pending_lock:
            if self._dead:
                raise CloudMcpError("cloud_mcp_disconnected")
            self._next_id += 1
            identifier = self._next_id
        # Encoding fails before a frame is sent, so this failure is certain.
        encoded = self._encode({"id": identifier, "method": method, "params": params})
        pending = queue.Queue(maxsize=1)
        with self._pending_lock:
            if self._dead:
                raise CloudMcpError("cloud_mcp_disconnected")
            self._pending[identifier] = pending
        threading.Thread(target=self._write_frame, args=(encoded,), daemon=True).start()
        try:
            result = pending.get(timeout=self._timeout)
        except queue.Empty:
            raise CloudMcpError("cloud_mcp_timeout", uncertain=mutating) from None
        finally:
            with self._pending_lock:
                self._pending.pop(identifier, None)
        if isinstance(result, CloudMcpError):
            raise CloudMcpError(result.code, uncertain=mutating)
        if "error" in result:
            raise CloudMcpError("cloud_mcp_rpc_error", uncertain=mutating)
        if "result" not in result:
            raise CloudMcpError("cloud_mcp_response_invalid", uncertain=mutating)
        return result["result"]

    def _fail_all(self, code):
        with self._pending_lock:
            self._dead = True
            for pending in self._pending.values():
                try:
                    pending.put_nowait(CloudMcpError(code))
                except queue.Full:
                    pass

    def _read(self, stream):
        try:
            while True:
                line = stream.readline(MAX_FRAME + 1)
                if not line:
                    self._fail_all("cloud_mcp_disconnected")
                    return
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    self._fail_all("cloud_mcp_frame_invalid")
                    return
                value = json.loads(line)
                if not isinstance(value, dict):
                    self._fail_all("cloud_mcp_frame_invalid")
                    return
                if "method" in value:
                    if "id" in value:
                        # No approval, OAuth, sampling, elicitation or server
                        # request receives an automatic response.
                        self._fail_all("cloud_mcp_server_input_required")
                        return
                    continue
                identifier = value.get("id")
                if type(identifier) is not int:
                    self._fail_all("cloud_mcp_frame_invalid")
                    return
                with self._pending_lock:
                    pending = self._pending.get(identifier)
                    if pending is not None:
                        try:
                            pending.put_nowait(value)
                        except queue.Full:
                            pass
        except Exception:
            self._fail_all("cloud_mcp_frame_invalid")

    def close(self):
        with self._pending_lock:
            healthy = self._ready and not self._dead and not self._pending
        self._ready = False
        self._fail_all("cloud_mcp_disconnected")
        process, self._process = self._process, None
        if process is None:
            return
        eof_finished = None
        graceful = False
        # Orderly stdin EOF lets a healthy app-server release its configured
        # MCP transports/children. Never wait for a stalled writer's lock.
        if healthy and process.poll() is None and self._write_lock.acquire(timeout=0.1):
            eof_finished = threading.Event()
            eof_failed = threading.Event()
            def finish_input():
                try:
                    process.stdin.close()
                except Exception:
                    eof_failed.set()
                finally:
                    self._write_lock.release()
                    eof_finished.set()
            deadline = time.monotonic() + 3
            threading.Thread(target=finish_input, daemon=True).start()
            if eof_finished.wait(max(0, deadline - time.monotonic())) and not eof_failed.is_set():
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                    graceful = True
                except Exception:
                    pass
        # Failure/timeout fallback only affects our no-turn helper. An active
        # writer is released by process termination, without blocking on close.
        try:
            if not graceful and process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=3)
            except Exception:
                pass
        streams = [process.stdout]
        if eof_finished is None or eof_finished.is_set():
            streams.append(process.stdin)
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

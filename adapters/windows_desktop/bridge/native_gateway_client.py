"""Bounded authenticated IPC to the existing Desktop engine's stdio proxy.

No engine is started here. Each mutation is sent once; a lost reply leaves an
explicit uncertain outcome and must be reconciled using its operation ID.
"""
from __future__ import annotations
import bridge_config as config

import ctypes
from dataclasses import dataclass
import json
from multiprocessing.connection import Client
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable
import uuid

SCHEMA = "codex-native-gateway/1"
MAX_FRAME = 16 * 1024 * 1024
MAX_MANIFEST = 64 * 1024
MAX_INSTANCES = 64
DEFAULT_DIRECTORY = config.runtime_dir() / "native_gateway"
DEFAULT_KEYS_DIRECTORY = DEFAULT_DIRECTORY.parent / "native_gateway_keys"
MUTATIONS = frozenset({"answer", "owner_input"})
COMMANDS = frozenset({"status", "list_requests"}) | MUTATIONS
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
_PIPE = re.compile(r"\\\\\.\\pipe\\codex-native-gateway-[A-Za-z0-9_.-]{1,160}\Z")
_EXCHANGES = threading.BoundedSemaphore(8)


class GatewayError(RuntimeError):
    """Stable code only: never expose the key, pipe or server exception text."""

    def __init__(self, code: str, *, uncertain: bool = False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


@dataclass(frozen=True)
class GatewayInstance:
    epoch: str
    pid: int
    pipe: str
    thread_ids: tuple[str, ...]
    authkey_file: Path


def _thread_id(value: Any) -> str:
    if not isinstance(value, str):
        raise GatewayError("native_gateway_thread_invalid")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise GatewayError("native_gateway_thread_invalid") from None


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    try:
        code = ctypes.c_uint32()
        return bool(kernel.GetExitCodeProcess(process, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(process)


def _decrypt_key(value: bytes) -> bytes:
    from telegram_bridge import unprotect
    return unprotect(value)


class NativeGatewayClient:
    def __init__(self, *, directory: Path | str | None = None,
                 keys_directory: Path | str | None = None,
                 timeout: float = 8.0,
                 decrypt_key: Callable[[bytes], bytes] = _decrypt_key,
                 process_alive: Callable[[int], bool] = _pid_alive,
                 connect: Callable[..., Any] = Client):
        if directory is None:
            from telegram_bridge import APP_DIR
            directory = APP_DIR / "native_gateway"
        self.directory = Path(directory).resolve()
        self.keys_directory = (Path(keys_directory).resolve() if keys_directory is not None
                               else self.directory.parent / "native_gateway_keys")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise GatewayError("native_gateway_timeout_invalid")
        self.timeout = float(timeout)
        self._decrypt_key = decrypt_key
        self._process_alive = process_alive
        self._connect = connect

    def instances(self) -> list[GatewayInstance]:
        try:
            paths = list(self.directory.glob("*.json"))
            if len(paths) > MAX_INSTANCES:
                raise GatewayError("native_gateway_instance_limit")
        except OSError:
            raise GatewayError("native_gateway_discovery_failed") from None
        result = []
        for path in paths:
            try:
                if path.stat().st_size > MAX_MANIFEST or path.is_symlink():
                    continue
                with path.open("rb") as stream:
                    raw = stream.read(MAX_MANIFEST + 1)
                if len(raw) > MAX_MANIFEST:
                    continue
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get("schema") != SCHEMA:
                    continue
                epoch = value.get("epoch")
                pid = value.get("pid")
                pipe = value.get("pipe")
                threads = value.get("thread_ids")
                if (not isinstance(epoch, str) or not _IDENTIFIER.fullmatch(epoch) or path.stem != epoch
                        or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
                        or not isinstance(pipe, str) or not _PIPE.fullmatch(pipe)
                        or not isinstance(threads, list) or len(threads) > 10000
                        or not all(isinstance(t, str) for t in threads)):
                    continue
                thread_ids = tuple(dict.fromkeys(_thread_id(t) for t in threads))
                key = Path(value.get("authkey_file", ""))
                expected = self.keys_directory / f"{epoch}.bin"
                if not key.is_absolute() or key.resolve() != expected or key.is_symlink():
                    continue
                if not key.is_file() or not 1 <= key.stat().st_size <= 16384:
                    continue
                if not self._process_alive(pid):
                    continue
                result.append(GatewayInstance(epoch, pid, pipe, thread_ids, key))
            except (OSError, ValueError, TypeError, GatewayError):
                continue
        return sorted(result, key=lambda instance: instance.epoch)

    def discover(self, thread_id: str | None = None) -> GatewayInstance:
        thread = None if thread_id is None else _thread_id(thread_id)
        instances = [instance for instance in self.instances()
                     if thread is None or thread in instance.thread_ids]
        if not instances:
            raise GatewayError("native_gateway_unavailable")
        if len(instances) != 1:
            raise GatewayError("native_gateway_ambiguous")
        return instances[0]

    def status(self, *, thread_id: str | None = None) -> dict:
        return self.call("status", thread_id=thread_id)

    def list_requests(self, *, thread_id: str | None = None) -> dict:
        return self.call("list_requests", thread_id=thread_id)

    def answer(self, *, thread_id: str, request_key: str,
               response: dict, operation_id: str) -> dict:
        return self.call("answer", thread_id=thread_id, operation_id=operation_id,
                         request_key=request_key, response=response)

    def owner_input(self, *, thread_id: str, prompt: str, operation_id: str) -> dict:
        return self.call("owner_input", thread_id=thread_id, operation_id=operation_id,
                         prompt=prompt)

    def call(self, command: str, *, thread_id: str | None = None,
             operation_id: str | None = None, **arguments: Any) -> dict:
        if command not in COMMANDS:
            raise GatewayError("native_gateway_command_invalid")
        mutation = command in MUTATIONS
        if mutation and (not isinstance(operation_id, str) or not _IDENTIFIER.fullmatch(operation_id)):
            raise GatewayError("native_gateway_operation_invalid")
        thread = None if thread_id is None else _thread_id(thread_id)
        if mutation and thread is None:
            raise GatewayError("native_gateway_thread_invalid")
        permitted = {"answer": {"request_key", "response"}, "owner_input": {"prompt"},
                     "status": set(), "list_requests": set()}[command]
        if set(arguments) != permitted:
            raise GatewayError("native_gateway_arguments_invalid")
        if command == "answer" and (not isinstance(arguments["request_key"], str)
                                     or not arguments["request_key"]
                                     or len(arguments["request_key"]) > 1024
                                     or not isinstance(arguments["response"], dict)):
            raise GatewayError("native_gateway_arguments_invalid")
        if command == "owner_input" and (not isinstance(arguments["prompt"], str)
                                         or not arguments["prompt"].strip()):
            raise GatewayError("native_gateway_arguments_invalid")
        envelope = {"version": 1, "command": command, **arguments}
        if thread is not None:
            envelope["thread_id"] = thread
        if operation_id is not None:
            envelope["operation_id"] = operation_id
        try:
            payload = json.dumps(envelope, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise GatewayError("native_gateway_arguments_invalid") from None
        if len(payload) > MAX_FRAME:
            raise GatewayError("native_gateway_request_too_large")
        instance = self.discover(thread)
        try:
            key = self._decrypt_key(instance.authkey_file.read_bytes())
            if not isinstance(key, bytes) or not 16 <= len(key) <= 256:
                raise ValueError()
        except Exception:
            raise GatewayError("native_gateway_key_unavailable") from None
        state: dict[str, Any] = {"sent": False, "abandoned": False}
        gate = threading.Lock()
        completed = threading.Event()
        if not _EXCHANGES.acquire(blocking=False):
            raise GatewayError("native_gateway_busy")

        def exchange():
            connection = None
            try:
                connection = self._connect(instance.pipe, family="AF_PIPE", authkey=key)
                with gate:
                    state["connection"] = connection
                    if state["abandoned"]:
                        return
                    # After this boundary even a write failure may have reached
                    # the proxy. It is never safe to retry a mutation blindly.
                    state["sent"] = True
                connection.send_bytes(payload)
                if not connection.poll(self.timeout):
                    raise GatewayError("native_gateway_timeout", uncertain=mutation)
                raw = connection.recv_bytes(MAX_FRAME)
                reply = json.loads(raw)
                if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
                    raise GatewayError("native_gateway_response_invalid", uncertain=mutation)
                if mutation and reply.get("operation_id") != operation_id:
                    raise GatewayError("native_gateway_correlation_invalid", uncertain=True)
                if reply["ok"]:
                    if not isinstance(reply.get("result"), dict):
                        raise GatewayError("native_gateway_response_invalid", uncertain=mutation)
                    state["result"] = reply["result"]
                else:
                    code = reply.get("error")
                    if not isinstance(code, str) or not _IDENTIFIER.fullmatch(code):
                        code = "native_gateway_rejected"
                    # Server must explicitly state a definite rejection. A
                    # generic internal error after submission is uncertain.
                    state["error"] = GatewayError(code, uncertain=mutation and reply.get("uncertain") is not False)
            except GatewayError as exc:
                state["error"] = exc
            except Exception:
                state["error"] = GatewayError("native_gateway_transport_failed",
                                               uncertain=mutation and state["sent"])
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                completed.set()
                _EXCHANGES.release()

        try:
            threading.Thread(target=exchange, daemon=True,
                             name=f"native-gateway-{command}").start()
        except Exception:
            _EXCHANGES.release()
            raise GatewayError("native_gateway_transport_failed") from None
        if not completed.wait(self.timeout):
            with gate:
                state["abandoned"] = True
                sent = state["sent"]
                connection = state.get("connection")
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise GatewayError("native_gateway_timeout", uncertain=mutation and sent)
        if "error" in state:
            raise state["error"]
        return state["result"]

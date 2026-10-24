"""On-demand Codex quota read. No model turns, auth-file access or polling."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import queue
import subprocess
import threading
import time


_SAFE_ERRORS = {"usage_unavailable", "usage_timeout", "usage_protocol_error",
                "invalid_usage", "usage_cache_error"}
_MAX_LINE = 1024 * 1024


class UsageError(Exception):
    def __init__(self, code="usage_unavailable"):
        super().__init__(code if code in _SAFE_ERRORS else "usage_unavailable")


def _window(value):
    if not isinstance(value, dict):
        raise UsageError("invalid_usage")
    used, duration, reset = (value.get(key) for key in ("usedPercent", "windowDurationMins", "resetsAt"))
    if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
        raise UsageError("invalid_usage")
    if type(duration) is not int or not 0 < duration <= 525600:
        raise UsageError("invalid_usage")
    if type(reset) is not int or not 1600000000 <= reset <= 4100000000:
        raise UsageError("invalid_usage")
    return {"usedPercent": used, "windowDurationMins": duration, "resetsAt": reset}


def sanitize_usage(value):
    """Return only codex and the six permitted window fields; primary is required."""
    if not isinstance(value, dict) or value.get("limitId") != "codex":
        raise UsageError("invalid_usage")
    primary = _window(value.get("primary"))
    secondary = value.get("secondary")
    return {"limitId": "codex", "primary": primary,
            "secondary": None if secondary is None else _window(secondary)}


def _from_response(response):
    if not isinstance(response, dict):
        raise UsageError("usage_protocol_error")
    buckets = response.get("rateLimitsByLimitId")
    bucket = buckets.get("codex") if isinstance(buckets, dict) else None
    if bucket is None:
        bucket = response.get("rateLimits")
    return sanitize_usage(bucket)


def read_codex_usage(timeout=12):
    """Read the installed CLI's account/rateLimits/read, then stop this read-only child.

    Only initialize, initialized and account/rateLimits/read are sent. No thread
    is loaded or started. Raw responses, stderr, accountId and credentials are
    never logged or returned. A deadline always kills/reaps this child.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise UsageError("usage_timeout")
    process, reader = None, None
    messages = queue.Queue(maxsize=64)
    overflow = threading.Event()
    deadline = time.monotonic() + timeout

    def consume():
        try:
            while True:
                line = process.stdout.readline(_MAX_LINE + 1)
                if not line:
                    item = UsageError("usage_unavailable")
                elif len(line) > _MAX_LINE:
                    item = UsageError("usage_protocol_error")
                else:
                    try: item = json.loads(line.decode("utf-8"))
                    except Exception: item = UsageError("usage_protocol_error")
                try: messages.put_nowait(item)
                except queue.Full: overflow.set(); return
                if isinstance(item, UsageError): return
        except Exception:
            try: messages.put_nowait(UsageError("usage_unavailable"))
            except queue.Full: overflow.set()

    def write(value):
        if time.monotonic() >= deadline: raise UsageError("usage_timeout")
        process.stdin.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        process.stdin.flush()

    def receive(identifier):
        while True:
            if overflow.is_set(): raise UsageError("usage_protocol_error")
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise UsageError("usage_timeout")
            try: value = messages.get(timeout=remaining)
            except queue.Empty: raise UsageError("usage_timeout") from None
            if isinstance(value, UsageError): raise value
            if not isinstance(value, dict): raise UsageError("usage_protocol_error")
            if "method" in value:
                # Never approve/execute an unsolicited server request.
                if "id" in value: raise UsageError("usage_protocol_error")
                continue
            if value.get("id") != identifier: continue
            if "error" in value: raise UsageError("usage_unavailable")
            if not isinstance(value.get("result"), dict): raise UsageError("usage_protocol_error")
            return value["result"]

    try:
        from wake_dispatcher import codex_binary
        process = subprocess.Popen([str(codex_binary()), "app-server", "--stdio"],
            cwd=str(Path(__file__).resolve().parent), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        reader = threading.Thread(target=consume, name="codex-usage-read", daemon=True)
        reader.start()
        write({"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{
            "clientInfo":{"name":"telegram_usage_readonly", "version":"1.0"},
            "capabilities":{"experimentalApi":True, "requestAttestation":False}}})
        receive(1)
        write({"jsonrpc":"2.0", "method":"initialized", "params":{}})
        write({"jsonrpc":"2.0", "id":2, "method":"account/rateLimits/read", "params":None})
        return _from_response(receive(2))
    except UsageError:
        raise
    except Exception:
        raise UsageError("usage_unavailable") from None
    finally:
        if process is not None:
            # This process never executes a task; ending it cannot interrupt a turn.
            cleanup_failed, reaped = False, False
            try:
                try:
                    if process.poll() is None: process.kill()
                    process.wait(timeout=3)
                    reaped = True
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                    reaped = True
            except Exception:
                cleanup_failed = True
            finally:
                # Buffered stdout.close() can wait for a reader blocked in
                # readline. Do not close/join streams of an unreaped process.
                if reaped:
                    for stream in (process.stdin, process.stdout):
                        try: stream.close()
                        except Exception: pass
                    if reader is not None: reader.join(1)
            if cleanup_failed:
                raise UsageError("usage_unavailable") from None


def _cached(value):
    usage = sanitize_usage(value)
    stamp = value.get("updated_at")
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
        raise UsageError("invalid_usage")
    return {**usage, "updated_at": stamp}


class _Flight:
    def __init__(self):
        self.done, self.result = threading.Event(), None


class UsageService:
    """Thread-safe snapshot/refresh. Concurrent refresh callers share one result.

    reader returns sanitize_usage-compatible data. load_cache returns a persisted
    payload or None; save_cache accepts a new sanitized payload with updated_at.
    No worker, background thread, timer or periodic refresh is created here.
    """
    def __init__(self, reader, load_cache, save_cache, clock=time.time):
        self.reader, self.load_cache, self.save_cache, self.clock = reader, load_cache, save_cache, clock
        self._lock = threading.RLock()
        self._usage, self._flight, self._refresh_error, self._last_success = None, None, None, None

    def _load(self):
        try: candidate = _cached(self.load_cache())
        except Exception: return
        if self._usage is None or candidate["updated_at"] >= self._usage["updated_at"]:
            self._usage = candidate

    def _snapshot(self):
        checked = self._usage["updated_at"] if self._usage else None
        age = self.clock() - checked if checked is not None else None
        return {"usage":copy.deepcopy(self._usage), "stale":age is None or age < 0 or age >= 180,
                "refreshing":self._flight is not None, "refreshError":self._refresh_error,
                "checkedAt":checked}

    def snapshot(self):
        with self._lock:
            self._load()
            return self._snapshot()

    def refresh(self):
        with self._lock:
            self._load()
            flight = self._flight
            if flight is None:
                elapsed = None if self._last_success is None else self.clock() - self._last_success
                if elapsed is not None and 0 <= elapsed < 5:
                    return {**self._snapshot(), "refreshed":False}
                flight = self._flight = _Flight()
                owner = True
            else:
                owner = False
        if not owner:
            flight.done.wait()
            return copy.deepcopy(flight.result)
        refreshed, error, payload, checked = False, None, None, None
        try:
            payload = sanitize_usage(self.reader())
            checked = self.clock()
            if type(checked) not in (int, float) or not math.isfinite(checked) or checked <= 0:
                raise UsageError("usage_unavailable")
            payload["updated_at"] = checked
            try: self.save_cache(copy.deepcopy(payload))
            except Exception: raise UsageError("usage_cache_error") from None
            refreshed = True
        except UsageError as exc:
            error = str(exc)
        except Exception:
            error = "usage_unavailable"
        finally:
            with self._lock:
                if refreshed:
                    self._usage, self._last_success = payload, checked
                self._refresh_error = error
                self._flight = None
                flight.result = {**self._snapshot(), "refreshed":refreshed}
                flight.done.set()
        return copy.deepcopy(flight.result)

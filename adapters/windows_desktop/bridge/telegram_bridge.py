"""Local Telegram transport for the Codex desktop bridge.

The agent processes only verified commands from ``pending``. The bot token is
encrypted with Windows DPAPI and stays outside the synced project directory.
"""

from __future__ import annotations
import bridge_config as config

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import threading
import urllib.error
import urllib.parse
import urllib.request

from cloud_transport import is_enabled as cloud_transport_enabled
from latency_trace import mark as mark_delivery

_RESPONSE_OBSERVER = None
_ACTIVITY_OBSERVER = None


APP_DIR = config.runtime_dir()
SECRET_DIR = config.data_dir() / "secrets"
STATE_PATH = APP_DIR / "state.bin"
TOKEN_PATH = SECRET_DIR / "bot_token.dpapi"
INBOX = APP_DIR / "inbox"
OUTBOX = APP_DIR / "outbox"
WATCH_PATH = APP_DIR / "watch.bin"
NOTICE_PATH = APP_DIR / "notice.bin"
VOICE_PATH = APP_DIR / "voice_pending.bin"
THREADS_CACHE = APP_DIR / "threads_cache.bin"
PROJECTS_CACHE = APP_DIR / "projects_cache.bin"
ARCHIVED_CACHE = APP_DIR / "archived_cache.bin"
USAGE_CACHE = APP_DIR / "usage_cache.bin"
DECISIONS_CACHE = APP_DIR / "decisions_cache.bin"
DECISION_PROMPTS = APP_DIR / "decision_prompts.bin"
THREAD_DETAILS = APP_DIR / "thread_details"
RESULTS_PATH = APP_DIR / "results.bin"
ACTIVE_STATUSES = {"active", "running", "inprogress", "in_progress", "processing"}
MSK = timezone(timedelta(hours=3))
ASR_PYTHON = config.data_dir() / "asr-venv/Scripts/python.exe"
ASR_MODEL = config.data_dir() / "models/small"
EXPECTED_USERNAME = ""
SERVICE_THREAD_ID = config.service_thread_id()
MODEL_OPTIONS = (
    "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
)
EFFORT_OPTIONS = ("low", "medium", "high", "xhigh", "max", "ultra")
MODEL_EFFORTS = {
    model: EFFORT_OPTIONS[:-1] if model.endswith("-luna") else EFFORT_OPTIONS
    for model in MODEL_OPTIONS
}
EFFORT_LABELS = {
    "low": "Низкая", "medium": "Средняя", "high": "Высокая",
    "xhigh": "Очень высокая", "max": "Максимальная", "ultra": "Ультра",
}


class OwnerNotPaired(RuntimeError):
    """No Telegram network attempt was made because no owner chat is paired."""


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect(data: bytes) -> bytes:
    source, keepalive = _blob(data)
    target = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def unprotect(data: bytes) -> bytes:
    source, keepalive = _blob(data)
    target = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def ensure_dirs() -> None:
    for path in (APP_DIR, INBOX, OUTBOX, THREAD_DETAILS):
        path.mkdir(parents=True, exist_ok=True)


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(protect(json.dumps(value, ensure_ascii=False).encode("utf-8")))
    temp.replace(path)


def read_json(path: Path):
    return json.loads(unprotect(path.read_bytes()).decode("utf-8"))


def state() -> dict:
    ensure_dirs()
    if STATE_PATH.exists():
        return read_json(STATE_PATH)
    value = {"pair_code": secrets.token_urlsafe(18), "offset": 0, "owner_user_id": None, "owner_chat_id": None, "selected_thread_id": None, "selected_project_id": None}
    atomic_json(STATE_PATH, value)
    return value


def save_state(value: dict) -> None:
    atomic_json(STATE_PATH, value)


def settings_text(current: dict) -> str:
    model = current.get("model")
    thinking = current.get("thinking")
    return "Модель: " + (model or "по умолчанию") + ". Интенсивность: " + (EFFORT_LABELS.get(thinking) or "по умолчанию") + "."


def settings_buttons() -> list:
    return [[{"text": "Выбрать модель", "callback_data": "models"}, {"text": "Интенсивность", "callback_data": "efforts"}], [{"text": "Главное меню", "callback_data": "menu"}]]


def show_models(current: dict) -> None:
    buttons = [[{"text": "По умолчанию", "callback_data": "model:default"}]]
    for index in range(0, len(MODEL_OPTIONS), 2):
        buttons.append([{"text": model, "callback_data": "model:" + model} for model in MODEL_OPTIONS[index:index + 2]])
    buttons.append([{"text": "Главное меню", "callback_data": "menu"}])
    send(settings_text(current) + "\nВыберите модель для следующих действий в Codex:", buttons)


def show_efforts(current: dict) -> None:
    model = current.get("model")
    if model not in MODEL_EFFORTS:
        send("Сначала выберите модель через /model. Тогда я покажу допустимую интенсивность для неё.", settings_buttons())
        return
    buttons = [[{"text": "По умолчанию", "callback_data": "effort:default"}]]
    allowed = MODEL_EFFORTS[model]
    for index in range(0, len(allowed), 2):
        buttons.append([{"text": EFFORT_LABELS[item], "callback_data": "effort:" + item} for item in allowed[index:index + 2]])
    buttons.append([{"text": "Главное меню", "callback_data": "menu"}])
    send(settings_text(current) + "\nВыберите интенсивность для " + model + ":", buttons)


def change_model(current: dict, value: str) -> bool:
    if value == "default":
        current["model"] = None
        current["thinking"] = None
    elif value in MODEL_EFFORTS:
        current["model"] = value
        if current.get("thinking") not in MODEL_EFFORTS[value]:
            current["thinking"] = None
    else:
        return False
    save_state(current)
    send("Настройки Codex сохранены. " + settings_text(current), settings_buttons())
    return True


def change_effort(current: dict, value: str) -> bool:
    if value == "default":
        current["thinking"] = None
    elif current.get("model") in MODEL_EFFORTS and value in MODEL_EFFORTS[current["model"]]:
        current["thinking"] = value
    else:
        return False
    save_state(current)
    send("Настройки Codex сохранены. " + settings_text(current), settings_buttons())
    return True


def token() -> str:
    if os.environ.get("CTD_BRIDGE_BOT_TOKEN"):
        return os.environ["CTD_BRIDGE_BOT_TOKEN"]
    if not TOKEN_PATH.exists():
        try:
            TOKEN_PATH.stat()
        except OSError as exc:
            raise RuntimeError("Bot token unavailable: " + type(exc).__name__ + " / " + str(getattr(exc, 'winerror', ''))) from None
        raise RuntimeError("Bot token is not configured; run setup")
    return unprotect(TOKEN_PATH.read_bytes()).decode("utf-8")


def api(method: str, payload: dict, *, timeout: int = 40) -> dict:
    url = "https://api.telegram.org/bot" + token() + "/" + method
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram {method} returned HTTP {exc.code}") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result.get('error_code', 'unknown')}")
    return result["result"]


def send(text: str, buttons: list | None = None) -> None:
    current = state()
    chat_id = current.get("owner_chat_id")
    if chat_id is None:
        raise OwnerNotPaired("owner_not_paired")
    if len(text.encode("utf-16-le")) // 2 > 4000:
        raise ValueError("Telegram message exceeds 4000 UTF-16 units")
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    api("sendMessage", payload)


def study_status_button() -> list:
    """Link only to a fresh, probed owner Mini App tunnel."""
    try:
        from desktop_transport import miniapp_status
        path = APP_DIR / "miniapp_status.bin"
        status = miniapp_status(lambda: read_json(path) if path.is_file() else {})
        if status["healthy"]:
            return [[{"text": "Открыть панель", "web_app": {
                "url": status["url"].rstrip("/")}}]]
    except Exception:
        pass
    return []


def configure_telegram_commands() -> None:
    """Expose the same owner-only shortcuts in Telegram's slash-command menu."""
    owner = state().get("owner_chat_id")
    if not owner:
        return
    from telegram_webhook_cli import BOT_COMMANDS
    api("setMyCommands", {"scope": {"type": "chat", "chat_id": owner}, "commands": BOT_COMMANDS})


def enqueue(update_id: int, kind: str, text: str, selected_thread_id: str | None = None, settings: dict | None = None, *, allow_fast: bool = False, attachments: list | None = None) -> str | None:
    path = INBOX / f"{update_id}.bin"
    if path.exists():
        return
    staged = INBOX / f"{update_id}.fast_pending"
    if staged.exists():
        return finish_fast_pending(staged)
    value = {"id": update_id, "kind": kind, "text": text[:4000], "selected_thread_id": selected_thread_id, "received_at": time.time()}
    if settings is not None:
        value["model"] = settings.get("model")
        value["thinking"] = settings.get("thinking")
    if attachments:
        value["attachments"] = attachments
    from fast_dispatch import candidate
    if allow_fast and candidate(value):
        # Never expose the original command to the model worker during dispatch.
        atomic_json(staged, value)
        return finish_fast_pending(staged)
    atomic_json(path, value)


def _acquire_fast_pending_lock(staged: Path):
    """Acquire one nonblocking OS lock; process exit releases ownership."""
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        identity = hashlib.sha256(str(staged.resolve()).casefold().encode("utf-8")).hexdigest()
        handle = kernel.CreateMutexW(None, False, "Local\\CtdTelegramFastFinish-" + identity)
        if not handle:
            raise OSError("fast_finish_lock_unavailable")
        outcome = kernel.WaitForSingleObject(handle, 0)
        if outcome == 258:  # WAIT_TIMEOUT: another process is finishing it.
            kernel.CloseHandle(handle)
            return None
        if outcome not in (0, 128):  # WAIT_OBJECT_0 or abandoned owner.
            kernel.CloseHandle(handle)
            raise OSError("fast_finish_lock_unavailable")
        def release():
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
        return release
    # The bridge runs on Windows; keep isolated regression runs portable.
    import fcntl
    lock_path = staged.with_suffix(".finish.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return None
    return stream.close


def finish_fast_pending(staged: Path) -> str:
    """Serialize dispatch, receipt and observer publication across processes."""
    release = _acquire_fast_pending_lock(staged)
    if release is None:
        return "busy"
    try:
        # Another owner may already have published and ACKed the operation.
        if not staged.is_file():
            return "completed"
        return _finish_fast_pending_locked(staged)
    finally:
        release()


def _finish_fast_pending_locked(staged: Path) -> str:
    """Resolve a staged command; attempted/uncertain dispatch never falls back."""
    from fast_dispatch import dispatch_pending
    value = read_json(staged)
    try:
        outcome = ({"status": "uncertain"} if value.get("fast_dispatch_blocked") else
                   dispatch_pending(value, verified_local_dispatch_rows(), ledger_path=APP_DIR / "fast_dispatch.sqlite"))
    except Exception:
        # If the ledger/result failed, do not infer that the executable never
        # ran. Persist a second stop guard before any subsequent recovery.
        value["fast_dispatch_blocked"] = True
        atomic_json(staged, value)
        outcome = {"status": "uncertain"}
    status = outcome["status"]
    path = INBOX / f"{value['id']}.bin"
    if status == "fallback":
        staged.replace(path)
        return status
    identifier = str(value["id"])
    results = read_json(RESULTS_PATH) if RESULTS_PATH.exists() else {}
    first_receipt = results.get(identifier, {}).get("dispatch_status") != status
    accepted = status in ("accepted", "accepted_wake_failed")
    text = ("Сообщение сохранено в очереди Codex. Проверяю, когда задача его примет." if status == "accepted" else
            "Сообщение сохранено в очереди Codex, но пробуждение приложения не подтверждено. Повторно не отправляйте его." if status == "accepted_wake_failed" else
            "Не удалось подтвердить отправку в Codex. Автоматический повтор остановлен, чтобы не создать дубль. Проверьте выбранный чат.")
    results[identifier] = {"text": text, "ok": accepted, "at": int(time.time()), "dispatch_status": status,
                           "scopeThreadId": value["selected_thread_id"]}
    if len(results) > 1000:
        results = dict(sorted(results.items(), key=lambda row: row[1]["at"])[-1000:])
    atomic_json(RESULTS_PATH, results)
    if first_receipt:
        atomic_json(OUTBOX / ("fast-" + identifier + ".bin"), {"text": text, "buttons": None})
        flush_outbox()
    if accepted:
        watch = {"id": value["id"], "kind": "watch_dispatched", "selected_thread_id": value["selected_thread_id"],
                          "text": "Сообщение сохранено в очереди Codex. Дождаться подтверждения доставки и ответа; повторно не отправлять.",
                          "received_at": value["received_at"], "dispatchStatus": status,
                          "expected_text_hash": hashlib.sha256(value["text"].encode("utf-8")).hexdigest(),
                          "observer": "directory_events"}
        if outcome.get("queueItemId"):
            watch["queueItemId"] = outcome["queueItemId"]
        if outcome.get("clientUserMessageId"):
            watch["clientUserMessageId"] = outcome["clientUserMessageId"]
        atomic_json(path, watch)
        if _RESPONSE_OBSERVER is not None:
            try:
                watching = _RESPONSE_OBSERVER.watch(watch)
                if not watching:
                    raise RuntimeError("response_observer_unavailable")
            except Exception:
                # Hash/time-only worker fallback can bind a different user's
                # same-text message. Keep this command guarded for diagnosis.
                watch["observer_registration_failed"] = True
                if path.exists():
                    atomic_json(path, watch)
        staged.unlink(missing_ok=True)
    return status


def verified_local_dispatch_rows() -> list:
    """Fresh read-only identity evidence, used only with the real Desktop wake."""
    now = int(time.time())
    rows = {row["id"]: row for row in cached_threads() if isinstance(row, dict) and isinstance(row.get("id"), str)}
    for row in local_codex_threads():
        rows[row["id"]] = {**row, "native_verification": "local_codex_database", "native_verified_at": now}
    return list(rows.values())


def pending_records() -> list:
    """Internal accessor for the event observer; never exposes records in logs."""
    records = [read_json(path) for path in sorted(INBOX.glob("*.bin"))]
    for record in records:
        if record.get("kind") == "watch_dispatched":
            # Old receipts lack the observer marker. Never release them to a
            # worker that can only correlate by text hash and timestamp.
            record["observer"] = "directory_events"
    return records


def ack_pending(operation_id) -> None:
    identifier = str(operation_id)
    if not identifier.isdecimal() or len(identifier) > 24:
        raise ValueError("Numeric operation ID required")
    (INBOX / (identifier + ".bin")).unlink(missing_ok=True)


def release_response_observer(operation_id) -> None:
    """Retain an observer failure for exact-ID diagnosis; never hash-fallback."""
    identifier = str(operation_id)
    if not identifier.isdecimal() or len(identifier) > 24:
        raise ValueError("Numeric operation ID required")
    path = INBOX / (identifier + ".bin")
    if not path.is_file():
        return
    record = read_json(path)
    if record.get("kind") != "watch_dispatched":
        return
    record["observer"] = "directory_events"
    record["observer_registration_failed"] = True
    atomic_json(path, record)


def pending_command_exists(operation_id: str | int) -> bool:
    """Internal wake accessor: check one durable command, never disclose its body."""
    identifier = str(operation_id).removeprefix("telegram_")
    if re.fullmatch(r"[0-9]{1,24}", identifier) is None:
        return False
    return (INBOX / (identifier + ".bin")).is_file()


def pending_requires_worker(operation_id: str | int) -> bool:
    """A live directory-event observer already owns ordinary text results."""
    identifier = str(operation_id).removeprefix("telegram_")
    if not re.fullmatch(r"[0-9]{1,24}", identifier):
        return False
    path = INBOX / (identifier + ".bin")
    if not path.is_file():
        return False
    record = read_json(path)
    return record.get("kind") != "watch_dispatched"


def prepare_web(kind: str, text: str, selected_thread_id: str, settings: dict | None = None, attachments: list | None = None) -> int:
    """Reserve an unpublished Mini App command; no CLI, worker or Telegram call."""
    ensure_dirs()
    limit = 12000 if kind == "followup" else 4000
    if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > limit:
        raise ValueError("Текст ответа превышает допустимый размер")
    for _ in range(8):
        operation_id = secrets.randbelow(8_000_000_000_000_000_000) + 1_000_000_000_000_000_000
        if not any((INBOX / (str(operation_id) + suffix)).exists() for suffix in (".bin", ".fast_pending", ".web_pending")):
            break
    else:
        raise RuntimeError("Unable to allocate operation ID")
    record = {"id": operation_id, "kind": kind, "text": text, "selected_thread_id": selected_thread_id, "received_at": time.time()}
    if settings is not None:
        record["model"] = settings.get("model")
        record["thinking"] = settings.get("thinking")
        if settings.get("decision_answer") is True:
            record["decision_answer"] = True
    if attachments:
        record["attachments"] = attachments
    atomic_json(INBOX / f"{operation_id}.web_pending", record)
    return operation_id


def finish_web(operation_id: int) -> int:
    """Publish only after the caller has durably saved its request-ID mapping."""
    from fast_dispatch import candidate
    staged = INBOX / f"{operation_id}.web_pending"
    fast_staged = INBOX / f"{operation_id}.fast_pending"
    try:
        if staged.exists():
            value = read_json(staged)
            staged.rename(fast_staged if candidate(value) else INBOX / f"{operation_id}.bin")
        if fast_staged.exists():
            finish_fast_pending(fast_staged)
    except FileNotFoundError:
        # A concurrent recovery finished this same durable reservation. Never
        # recreate the original command, even when its ordinary pending is ACKed.
        pass
    from wake_dispatcher import notify_pending
    notify_pending(operation_id)
    return operation_id


def enqueue_web(kind: str, text: str, selected_thread_id: str, settings: dict | None = None, attachments: list | None = None) -> int:
    """Queue internal panel work that has no external client idempotency key."""
    return finish_web(prepare_web(kind, text, selected_thread_id, settings, attachments))


def local_codex_threads() -> list[dict]:
    """Read only user-facing local Codex metadata; merge live Desktop rows separately."""
    root = config.codex_home()
    databases = sorted(root.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1)
    if not databases:
        return []
    try:
        connection = sqlite3.connect(databases[-1].as_uri() + "?immutable=1", uri=True)
        try:
            rows = connection.execute(
                "SELECT id,title,updated_at,updated_at_ms,cwd,project_id,is_pinned "
                "FROM threads WHERE archived=0 AND thread_source IN "
                "('user','chatgpt_handoff','avatar_quick_chat','agent_forked_thread')"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return []
    result = []
    for thread_id, title, updated_at, updated_at_ms, cwd, project_id, pinned in rows:
        if not config.allowed_workspace(cwd) or not isinstance(thread_id, str) or thread_id in {
                SERVICE_THREAD_ID, "a0f78f20-6aef-5b55-adc5-f8f383fe6996"}:
            continue
        timestamp = int(updated_at_ms / 1000) if isinstance(updated_at_ms, int) and updated_at_ms > 0 else int(updated_at or 0)
        result.append({"id": thread_id, "kind": "codex", "title": title or "Чат Codex", "status": "unknown", "updatedAt": timestamp, "hostId": "local", "projectId": project_id, "cwd": cwd, "pinned": bool(pinned)})
    return result


def operation_result(operation_id: int) -> dict | None:
    rows = read_json(RESULTS_PATH) if RESULTS_PATH.exists() else {}
    result = rows.get(str(operation_id))
    if not isinstance(result, dict):
        return None
    scope_id = result.get("scopeThreadId")
    if result.get("scopeGeneric") is True and "scopeThreadId" not in result:
        return result  # Explicit trusted-worker marker; never infer this for legacy rows.
    if not isinstance(scope_id, str) or not thread_in_scope(scope_id):
        # Keep legacy data on disk, but never reveal a result with unknown backing kind.
        return {"ok": False, "scopeBlocked": True,
                "text": "Содержимое результата скрыто: чат не подтверждён.",
                "at": result.get("at")}
    return result


def preliminary_plan(row: dict, previous: dict | None, now: int) -> dict | None:
    """Estimate a finish interval from a new activity trigger, never as a promise."""
    status = str(row.get("status") or "").casefold()
    if status not in ACTIVE_STATUSES:
        return None
    old = previous or {}
    existing = old.get("plan")
    if (old.get("status") == row.get("status") and old.get("updatedAt") == row.get("updatedAt")
            and isinstance(existing, dict) and type(existing.get("earliest")) is int
            and type(existing.get("latest")) is int and existing["earliest"] <= existing["latest"]):
        return existing
    progressed = str(old.get("status") or "").casefold() in ACTIVE_STATUSES
    return {
        "triggerAt": now,
        "earliest": now + (10 if progressed else 20) * 60,
        "latest": now + (30 if progressed else 45) * 60,
        "basis": "activity" if progressed else "start",
    }


def plan_text(row: dict, now: int | None = None) -> str:
    plan = row.get("plan")
    if not isinstance(plan, dict) or type(plan.get("earliest")) is not int or type(plan.get("latest")) is not int:
        return ""
    now = int(time.time()) if now is None else now
    display = lambda timestamp: datetime.fromtimestamp(timestamp, MSK).strftime("%d.%m %H:%M")
    if now > plan["latest"]:
        return "Предварительный срок до " + display(plan["latest"]) + " МСК истёк; нужна новая оценка"
    return "Плановый ориентир: " + display(plan["earliest"]) + "–" + display(plan["latest"]) + " МСК"


def work_scope_ids() -> set:
    from work_scope import load_ids
    return load_ids(APP_DIR / "work_scope")


def activity_coverage(thread_id: str) -> dict:
    """Diagnose AgentMessage observation only, without terminal-state claims."""
    if not isinstance(thread_id, str) or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", thread_id) is None:
        return {"alive": False, "covered": False}
    observer = _ACTIVITY_OBSERVER
    if observer is None:
        return {"alive": False, "covered": False}
    try:
        return observer.coverage(thread_id)
    except Exception:
        return {"alive": False, "covered": False}


def known_chatgpt_ids(extra_rows=()) -> set:
    """Remember native ChatGPT backing kinds even when their content is hidden."""
    rows = list(extra_rows)
    for path in (THREADS_CACHE, ARCHIVED_CACHE):
        if path.exists():
            rows.extend(read_json(path).get("threads", []))
    return {row["id"] for row in rows if isinstance(row, dict) and row.get("kind") == "chatgpt"
            and isinstance(row.get("id"), str) and row["id"]}


def scope_rows(rows) -> list:
    from work_scope import allowed
    if not isinstance(rows, list):
        return []
    ids = work_scope_ids()
    chatgpt_ids = known_chatgpt_ids(rows)
    return [row for row in rows if allowed(row, ids)
            and not (row["kind"] == "codex" and row["id"] in chatgpt_ids)]


def preserve_scope_metadata(rows: list, chatgpt_ids: set) -> list:
    """Only id/kind survive for hidden chats; no title, content or status is copied."""
    present = {row["id"] for row in rows}
    return [*rows, *({"id": thread_id, "kind": "chatgpt"} for thread_id in sorted(chatgpt_ids - present))]


def thread_in_scope(thread_id: str) -> bool:
    """Use stored native backing kind, never a kind attached to a notice."""
    from work_scope import allowed
    if not isinstance(thread_id, str) or not thread_id:
        return False
    ids = work_scope_ids()
    known = []
    for path in (THREADS_CACHE, ARCHIVED_CACHE):
        if path.exists():
            known.extend(row for row in read_json(path).get("threads", [])
                         if isinstance(row, dict) and row.get("id") == thread_id)
    if known:
        return all(allowed(row, ids) for row in known)
    if thread_id in ids:
        return True  # Exact native-created Work proof can precede cache refresh.
    return any(row.get("id") == thread_id and allowed(row, ids) for row in local_codex_threads())


def require_thread_scope(thread_id: str) -> None:
    if not thread_in_scope(thread_id):
        raise ValueError("thread_out_of_scope")


def cache_snapshot(kind: str, encoded: str) -> None:
    payload = json.loads(urllib.parse.unquote(encoded))
    if kind == "cache_threads":
        if not isinstance(payload, list) or len(payload) > 5000:
            raise ValueError("Invalid thread cache")
        chatgpt_ids = known_chatgpt_ids(payload)
        previous_rows = {row.get("id"): row for row in cached_threads() if isinstance(row, dict)}
        now = int(time.time())
        rows = []
        for item in scope_rows(payload):
            if len(item["id"]) > 100:
                continue
            row = {key: item.get(key) for key in ("id", "kind", "title", "status", "updatedAt", "hostId", "projectId", "cwd", "pinned")}
            # Trust boundary: cache_threads accepts only fresh native list_threads output.
            # It is not a public Mini App command. Previously known ChatGPT backing
            # kinds cannot be relabelled as Codex. SQL-merged rows intentionally have
            # no native provenance and cannot use fast dispatch.
            row["native_verification"] = "codex_native_tools"
            row["native_verified_at"] = now
            row["plan"] = preliminary_plan(row, previous_rows.get(row["id"]), now)
            rows.append(row)
        known = {item["id"] for item in rows}
        rows.extend(item for item in local_codex_threads()
                    if item["id"] not in known and item["id"] not in chatgpt_ids)
        def stamp(item):
            try:
                return float(item.get("updatedAt") or 0)
            except (TypeError, ValueError):
                return 0
        rows.sort(key=stamp, reverse=True)
        atomic_json(THREADS_CACHE, {"updated_at": int(time.time()),
                                   "threads": preserve_scope_metadata(rows, chatgpt_ids)})
    elif kind == "cache_projects":
        if not isinstance(payload, list) or len(payload) > 100:
            raise ValueError("Invalid project cache")
        rows = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("projectId"), str) or len(item["projectId"]) > 100:
                continue
            rows.append({key: item.get(key) for key in ("projectId", "projectKind", "label", "hostId", "path")})
        atomic_json(PROJECTS_CACHE, {"updated_at": int(time.time()), "projects": rows})
    elif kind == "cache_archived":
        if not isinstance(payload, list) or len(payload) > 100:
            raise ValueError("Invalid archived cache")
        rows = []
        for item in scope_rows(payload):
            if len(item["id"]) > 100:
                continue
            rows.append({key: item.get(key) for key in ("id", "kind", "title", "status", "updatedAt", "hostId")})
        atomic_json(ARCHIVED_CACHE, {"updated_at": int(time.time()),
                                    "threads": preserve_scope_metadata(rows, known_chatgpt_ids(payload))})
    elif kind == "cache_thread":
        if not isinstance(payload, dict) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", str(payload.get("id") or "")):
            raise ValueError("Invalid thread detail")
        require_thread_scope(payload["id"])
        path = THREAD_DETAILS / (payload["id"] + ".bin")
        current = read_json(path).get("detail", {}) if path.exists() else {}
        older = current.get("messages") or []
        recent = payload.get("messages") or []
        if older and isinstance(recent, list):
            def message_key(item):
                if item.get("id") is not None:
                    return ("id", str(item["id"]))
                return ("content", json.dumps(item, ensure_ascii=False, sort_keys=True))
            merged = [item for item in older if isinstance(item, dict)]
            positions = {message_key(item): index for index, item in enumerate(merged)}
            for item in recent:
                if not isinstance(item, dict):
                    continue
                key = message_key(item)
                if key in positions:
                    merged[positions[key]] = item
                else:
                    positions[key] = len(merged)
                    merged.append(item)
            payload["messages"] = merged
            payload["nextCursor"] = current.get("nextCursor")
        atomic_json(path, {"updated_at": int(time.time()), "detail": payload})
    elif kind == "cache_thread_page":
        if not isinstance(payload, dict) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", str(payload.get("id") or "")):
            raise ValueError("Invalid thread page")
        require_thread_scope(payload["id"])
        path = THREAD_DETAILS / (payload["id"] + ".bin")
        current = read_json(path).get("detail", {}) if path.exists() else {}
        if current.get("nextCursor") != payload.get("cursor"):
            raise ValueError("Thread history cursor changed")
        older = payload.get("messages") or []
        if not isinstance(older, list) or len(older) > 30:
            raise ValueError("Invalid thread messages")
        existing = current.get("messages") or []
        known = {item.get("id") for item in older if isinstance(item, dict)}
        current["messages"] = older + [item for item in existing if isinstance(item, dict) and item.get("id") not in known]
        current["nextCursor"] = payload.get("nextCursor")
        atomic_json(path, {"updated_at": int(time.time()), "detail": current})
    else:
        raise ValueError("Unknown cache kind")


def cache_decision(encoded: str) -> None:
    """Store only the owner-decision flag for the latest inspected thread version."""
    payload = json.loads(urllib.parse.unquote(encoded))
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or len(payload["id"]) > 100:
        raise ValueError("Invalid decision record")
    if type(payload.get("needsDecision")) is not bool:
        raise ValueError("Invalid decision flag")
    thread = next((row for row in cached_threads() if row.get("id") == payload["id"]), None)
    if thread is None or thread.get("updatedAt") != payload.get("updatedAt"):
        raise ValueError("Decision thread version is stale")
    records = read_json(DECISIONS_CACHE) if DECISIONS_CACHE.exists() else {}
    records[payload["id"]] = {"updatedAt": thread.get("updatedAt"), "needsDecision": payload["needsDecision"], "checkedAt": int(time.time())}
    if len(records) > 1000:
        records = dict(sorted(records.items(), key=lambda item: item[1]["checkedAt"])[-1000:])
    atomic_json(DECISIONS_CACHE, records)
    print('{"cached":true}')


def register_decision(encoded: str) -> None:
    """Persist and publish an exact owner question, with stable native identity."""
    from decision_service import register
    result = register(sys.modules[__name__], json.loads(urllib.parse.unquote(encoded)))
    print(json.dumps(result, ensure_ascii=False))


def handle_decision_answer(update_id: int, data: str) -> None:
    match = re.fullmatch(r"answer:([a-f0-9]{20}):([0-9]{1,2})", data)
    if not match:
        send("Неизвестная кнопка ответа.")
        return
    from decision_service import answer
    try:
        result = answer(sys.modules[__name__], match[1], choice_id=match[2])
    except ValueError as exc:
        send("Ответ не отправлен: " + str(exc) + ". Откройте /decisions.")
        return
    send(decision_confirmation(result))


def decision_confirmation(result):
    stage = (result.get("decision") or {}).get("status") if isinstance(result, dict) else None
    if stage == "accepted_by_server":
        return "Codex принял ваш ответ на исходный запрос. Повторно отправлять его не нужно."
    if stage == "closed":
        return "Этот запрос уже закрыт в Desktop. Ответ повторно не отправлялся."
    if stage == "waiting_answers":
        return "Ответ сохранён. Ответьте на остальные вопросы этого запроса — затем все ответы будут переданы вместе."
    return "Ответ сохранён для исходного вопроса. Проверяю приём в Codex; повторно отправлять его не нужно."


def show_open_decision(decision_id: str) -> None:
    from decision_service import show
    try:
        show(sys.modules[__name__], decision_id)
    except ValueError:
        send("Этот запрос решения уже закрыт или недоступен.")


def cache_usage(encoded: str) -> None:
    """Keep only account-wide Codex window figures from the Desktop tool."""
    payload = json.loads(urllib.parse.unquote(encoded))
    if not isinstance(payload, dict) or payload.get("limitId") != "codex":
        raise ValueError("Invalid usage limit")
    windows = {}
    for name in ("primary", "secondary"):
        source = payload.get(name)
        if source is None:
            windows[name] = None
            continue
        if not isinstance(source, dict):
            raise ValueError("Invalid usage window")
        used = source.get("usedPercent")
        duration = source.get("windowDurationMins")
        reset = source.get("resetsAt")
        if type(used) not in (int, float) or not 0 <= used <= 100:
            raise ValueError("Invalid usage percentage")
        if type(duration) is not int or duration <= 0 or duration > 525600:
            raise ValueError("Invalid usage duration")
        if type(reset) is not int or reset < 1_600_000_000 or reset > 4_100_000_000:
            raise ValueError("Invalid usage reset")
        windows[name] = {"usedPercent": used, "windowDurationMins": duration, "resetsAt": reset}
    atomic_json(USAGE_CACHE, {"updated_at": int(time.time()), "limitId": "codex", **windows})
    print('{"cached":true}')


def transcribe_voice(file_id: str) -> str:
    """Download one owner voice message and transcribe it locally."""
    file_info = api("getFile", {"file_id": file_id})
    file_path = file_info.get("file_path")
    if not isinstance(file_path, str) or not file_path.startswith("voice/"):
        raise ValueError("Invalid Telegram voice path")
    url = "https://api.telegram.org/file/bot" + token() + "/" + urllib.parse.quote(file_path, safe="/")
    with tempfile.TemporaryDirectory(prefix="telegram_voice_") as folder:
        audio_path = Path(folder) / "voice.ogg"
        with urllib.request.urlopen(url, timeout=60) as response, audio_path.open("wb") as output:
            while chunk := response.read(65536):
                output.write(chunk)
                if output.tell() > 20_000_000:
                    raise ValueError("Voice file too large")
        result = subprocess.run(
            [str(ASR_PYTHON), str(Path(__file__).with_name("transcribe_voice.py")), str(ASR_MODEL), str(audio_path)],
            capture_output=True, timeout=240, check=True, encoding="utf-8", errors="replace",
        )
        transcript = json.loads(result.stdout).get("text", "").strip()
    if not transcript or len(transcript) > 4000:
        raise ValueError("Empty or overlong transcription")
    return transcript


def download_attachment(file_id: str, file_name: str) -> dict:
    """Store one owner-sent Telegram file in the managed attachment registry."""
    from miniapp_server import store_file
    info = api("getFile", {"file_id": file_id})
    file_path = info.get("file_path")
    if not isinstance(file_path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,300}", file_path) or ".." in file_path.split("/"):
        raise ValueError("Invalid Telegram attachment path")
    url = "https://api.telegram.org/file/bot" + token() + "/" + urllib.parse.quote(file_path, safe="/")
    chunks = []
    size = 0
    with urllib.request.urlopen(url, timeout=60) as response:
        while chunk := response.read(65536):
            size += len(chunk)
            if size > 20_000_000:
                raise ValueError("Attachment too large")
            chunks.append(chunk)
    return store_file(file_name, b"".join(chunks))


def voice_pending() -> dict:
    if not VOICE_PATH.exists():
        return {}
    previous = read_json(VOICE_PATH)
    current = {key: value for key, value in previous.items() if time.time() - value.get("at", 0) <= 86400}
    if len(current) != len(previous):
        atomic_json(VOICE_PATH, current)
    return current


def cached_threads() -> list:
    return scope_rows(read_json(THREADS_CACHE).get("threads", [])) if THREADS_CACHE.exists() else []


def cached_archived() -> list:
    return scope_rows(read_json(ARCHIVED_CACHE).get("threads", [])) if ARCHIVED_CACHE.exists() else []


def cached_projects() -> list:
    return [row for row in read_json(PROJECTS_CACHE).get("projects", []) if config.project_allowed(row)] if PROJECTS_CACHE.exists() else []


def show_cached_chats(page: int = 0, *, active_only: bool = False) -> None:
    rows = cached_threads()
    if active_only:
        rows = [row for row in rows if str(row.get("status") or "").casefold() in ("active", "running", "inprogress", "in_progress")]
    if not rows:
        send("Активных задач сейчас нет." if active_only else "Список чатов пока обновляется. Повторите через минуту.", study_status_button() + [[{"text": "Главное меню", "callback_data": "menu"}]])
        return
    page = max(0, min(page, (len(rows) - 1) // 10))
    shown = rows[page * 10:(page + 1) * 10]
    label = "Активные задачи" if active_only else "Чаты"
    buttons = []
    for row in shown:
        kind = "ChatGPT" if str(row.get("kind") or "").casefold() == "chatgpt" else "Codex"
        title = str(row.get("title") or "Без названия").replace("\n", " ")[:75]
        buttons.append([{"text": f"{kind}: {title[:38]}", "callback_data": "select:" + row["id"]}])
    navigation = []
    if page > 0:
        navigation.append({"text": "← Назад", "callback_data": "chats:page:" + str(page - 1)})
    if (page + 1) * 10 < len(rows):
        navigation.append({"text": "Далее →", "callback_data": "chats:page:" + str(page + 1)})
    if navigation:
        buttons.append(navigation)
    buttons.extend(study_status_button())
    buttons.append([{"text": "Главное меню", "callback_data": "menu"}])
    send(f"{label} · {page + 1}/{(len(rows) + 9) // 10}. Нажмите нужный чат:", buttons)


def show_cached_decisions(page: int = 0) -> None:
    prompts = read_json(DECISION_PROMPTS) if DECISION_PROMPTS.exists() else {}
    opened = [(key, value) for key, value in prompts.items()
              if isinstance(value, dict) and value.get("status") in {"open", "notification_uncertain", "desktop_required"}
              and thread_in_scope(value.get("threadId"))]
    if opened:
        opened.sort(key=lambda pair: pair[1].get("at", 0), reverse=True)
        page = max(0, min(page, (len(opened) - 1) // 8))
        buttons = [[{"text": str(value.get("title") or "Чат")[:50],
                     "callback_data": "decision:" + key}]
                   for key, value in opened[page * 8:(page + 1) * 8]]
        navigation = []
        if page > 0:
            navigation.append({"text": "← Назад", "callback_data": "decisions:page:" + str(page - 1)})
        if (page + 1) * 8 < len(opened):
            navigation.append({"text": "Далее →", "callback_data": "decisions:page:" + str(page + 1)})
        if navigation:
            buttons.append(navigation)
        buttons.append([{"text": "Главное меню", "callback_data": "menu"}])
        send(f"Ожидают ответа: {len(opened)} · страница {page + 1}/{(len(opened) + 7) // 8}. Выберите вопрос:", buttons)
        return
    flags = read_json(DECISIONS_CACHE) if DECISIONS_CACHE.exists() else {}
    rows = [row for row in cached_threads() if isinstance(flags.get(row.get("id")), dict)
            and flags[row["id"]].get("needsDecision") is True
            and flags[row["id"]].get("updatedAt") == row.get("updatedAt")]
    if not rows:
        send("Подтверждённых запросов решения сейчас нет. Список обновляется по новым сообщениям агентов.", [[{"text": "Главное меню", "callback_data": "menu"}]])
        return
    page = max(0, min(page, (len(rows) - 1) // 10))
    shown = rows[page * 10:(page + 1) * 10]
    buttons = []
    for row in shown:
        kind = "ChatGPT" if str(row.get("kind") or "").casefold() == "chatgpt" else "Codex"
        title = str(row.get("title") or "Без названия").replace("\n", " ")[:75]
        buttons.append([{"text": f"{kind}: {title[:38]}", "callback_data": "select:" + row["id"]}])
    navigation = []
    if page > 0:
        navigation.append({"text": "← Назад", "callback_data": "decisions:page:" + str(page - 1)})
    if (page + 1) * 10 < len(rows):
        navigation.append({"text": "Далее →", "callback_data": "decisions:page:" + str(page + 1)})
    if navigation:
        buttons.append(navigation)
    buttons.append([{"text": "Главное меню", "callback_data": "menu"}])
    send(f"Нужны ваши решения · {page + 1}/{(len(rows) + 9) // 10}. Нажмите нужный чат:", buttons)


def show_menu(current: dict) -> None:
    buttons = [
        [{"text": "📁 Проекты", "callback_data": "projects"}, {"text": "💬 Чаты", "callback_data": "chats"}],
        [{"text": "🧠 Модели", "callback_data": "models"}, {"text": "⚙️ Интенсивность", "callback_data": "efforts"}],
        [{"text": "⏳ В работе", "callback_data": "active"}, {"text": "❗ Нужны решения", "callback_data": "decisions"}],
        [{"text": "Настройки", "callback_data": "settings"}, {"text": "Справка", "callback_data": "help"}],
    ]
    buttons.extend(study_status_button())
    send("Панель задач. " + settings_text(current) + " Выберите действие:", buttons)


def show_cached_projects() -> None:
    rows = cached_projects()
    if not rows:
        send("Список проектов обновляется.", [[{"text": "Главное меню", "callback_data": "menu"}]])
        return
    buttons = [[{"text": str(p.get("label") or p["projectId"])[:60], "callback_data": "newproject:" + p["projectId"]}] for p in rows[:40]]
    buttons.append([{"text": "Главное меню", "callback_data": "menu"}])
    send("Выберите проект для новой задачи. Для ChatGPT доступна задача Work; модель и интенсивность выбираются для Codex.", buttons)


def show_cached_selection(thread_id: str) -> None:
    row = next((x for x in cached_threads() if x.get("id") == thread_id), None)
    if row is None:
        send("Чат не найден в текущем списке. Откройте /chats ещё раз.")
        return
    kind = "ChatGPT" if str(row.get("kind") or "").casefold() == "chatgpt" else "Codex"
    title = str(row.get("title") or "Без названия")[:150]
    estimate = plan_text(row)
    send(f"Выбран чат [{kind}] {title}. Статус: {row.get('status') or 'unknown'}. "
         + (estimate + ". " if estimate else "")
         + "Отправьте текст или файл; уточнение будет передано в этот чат.",
         study_status_button())


def queued_task_receipt() -> None:
    send("Задание получено компьютером. Передаю в выбранный чат; подтверждение и результат появятся после ответа Desktop.")


def settings_for_target(current: dict, kind: str, target_id: str) -> dict:
    rows = cached_projects() if kind == "new_task" else cached_threads()
    id_key = "projectId" if kind == "new_task" else "id"
    kind_key = "projectKind" if kind == "new_task" else "kind"
    target = next((row for row in rows if row.get(id_key) == target_id), None)
    if target and "chatgpt" in str(target.get(kind_key, "")).casefold():
        return {"model": None, "thinking": None}
    return current


def handle_text(text: str, update_id: int, current: dict) -> None:
    text = text.strip()
    if text == "/menu" or text == "/start":
        show_menu(current)
    elif text == "/help":
        send("/menu — быстрые действия; /projects — проекты; /model и /effort — модель и интенсивность Codex; /status — задачи в работе; /decisions — чаты, где явно требуется ваше решение; /chats — все чаты. После выбора чата отправьте уточнение текстом или голосом. Новую задачу создайте через /projects и /new текст. Для ChatGPT выбор модели и интенсивности недоступен.", [[{"text": "Главное меню", "callback_data": "menu"}]])
    elif text == "/settings":
        send(settings_text(current) + " Настройки применяются к следующим действиям в Codex.", settings_buttons())
    elif text == "/model":
        show_models(current)
    elif text == "/effort":
        show_efforts(current)
    elif text == "/chats":
        show_cached_chats()
    elif text == "/projects":
        show_cached_projects()
    elif text in ("/active", "/status"):
        show_cached_chats(active_only=True)
    elif text == "/decisions":
        show_cached_decisions()
    elif text.startswith("/new "):
        if current.get("selected_project_id"):
            enqueue(update_id, "new_task", text[5:].strip(), current["selected_project_id"], settings_for_target(current, "new_task", current["selected_project_id"]))
            queued_task_receipt()
        else:
            send("Сначала выберите проект через /projects.")
    elif text.startswith("/"):
        enqueue(update_id, "command", text[1:])
    elif not current.get("selected_thread_id"):
        send("Сначала выберите чат через /chats.")
    elif not thread_in_scope(current["selected_thread_id"]):
        send("Этот чат исключён из Telegram-панели. Выберите Codex или подтверждённую Work-задачу через /chats.")
    else:
        dispatch_status = enqueue(update_id, "followup", text, current["selected_thread_id"], settings_for_target(current, "followup", current["selected_thread_id"]), allow_fast=True)
        if dispatch_status not in ("accepted", "accepted_wake_failed", "uncertain"):
            queued_task_receipt()


def interpret_voice(text: str) -> str:
    simple = re.sub(r"[.!?]+$", "", text.casefold().strip())
    commands = {
        "покажи чаты": "/chats", "список чатов": "/chats", "покажи проекты": "/projects",
        "активные задачи": "/status", "покажи активные задачи": "/status",
        "статус задач": "/status", "запросы в работе": "/status",
        "нужны решения": "/decisions", "где нужно мое решение": "/decisions",
        "меню": "/menu", "настройки": "/settings", "модель": "/model",
        "интенсивность": "/effort",
    }
    if simple in commands:
        return commands[simple]
    for prefix in ("новая задача ", "создай задачу "):
        if simple.startswith(prefix):
            return "/new " + text.strip()[len(prefix):]
    return text


def process_update(update: dict, *, acknowledge_callback: bool = True) -> None:
    current = state()
    update_id = int(update["update_id"])
    callback = update.get("callback_query")
    message = update.get("message") or (callback or {}).get("message") or {}
    user = (callback or {}).get("from") or message.get("from") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return
    user_id = user.get("id")
    chat_id = chat.get("id")
    if current.get("owner_user_id") is None:
        command = str(message.get("text") or "")
        parts = command.split(maxsplit=1)
        if (
            len(parts) == 2
            and parts[0].startswith("/start")
            and secrets.compare_digest(parts[1].strip(), current["pair_code"])
            and config.owner_matches(user)
            and user_id == chat_id
        ):
            current["owner_user_id"] = user_id
            current["owner_chat_id"] = chat_id
            current["pair_code"] = None
            save_state(current)
            try:
                configure_telegram_commands()
            except Exception:
                pass
            try:
                send("Связь с этим компьютером установлена. Напишите /menu.")
            finally:
                # Newly paired owners can receive saved notices, and the
                # background worker can retry questions deferred before pairing.
                try:
                    flush_outbox()
                except Exception:
                    pass
                try:
                    from wake_dispatcher import notify_pending
                    notify_pending()
                except Exception:
                    pass
        return
    if (
        user_id != current["owner_user_id"]
        or chat_id != current["owner_chat_id"]
        or not config.owner_matches(user)
    ):
        return
    if callback and callback.get("id") and acknowledge_callback:
        try:
            api("answerCallbackQuery", {"callback_query_id": callback["id"]}, timeout=4)
        except Exception as exc:
            # Telegram's callback spinner is cosmetic; its ACK must never
            # prevent the owner's answer from reaching the durable decision path.
            try:
                if sys.stderr is not None:
                    print(f"Callback ACK failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            except Exception:
                pass
    from desktop_menu import BridgeBackend, handle_pending, record_from_update
    menu_record = record_from_update(update)
    if menu_record is not None:
        menu_result = handle_pending(menu_record, backend=BridgeBackend(sys.modules[__name__]),
            ledger_path=APP_DIR / "desktop_menu.sqlite3", authenticated=True)
        if menu_result.get("handled"):
            if not menu_result.get("ok"):
                raise RuntimeError("desktop_menu_operation_uncertain")
            return
    if callback:
        data = str(callback.get("data") or "")
        if data.startswith("answer:"):
            handle_decision_answer(update_id, data)
        elif data.startswith("custom:"):
            from decision_service import prompt_custom
            try:
                prompt_custom(sys.modules[__name__], data.split(":", 1)[1])
            except (ValueError, RuntimeError) as exc:
                send("Не удалось открыть поле ответа: " + str(exc) + ". Откройте /decisions.")
        elif data.startswith("decision:"):
            show_open_decision(data.split(":", 1)[1])
        elif data.startswith("voice_send:") or data.startswith("voice_cancel:"):
            action, nonce = data.split(":", 1)
            pending = voice_pending()
            entry = pending.pop(nonce, None)
            if entry is None or time.time() - entry.get("at", 0) > 86400:
                send("Голосовая команда уже обработана или устарела.")
                return
            atomic_json(VOICE_PATH, pending)
            if action == "voice_cancel":
                send("Голосовая команда отменена.")
            else:
                handle_text(interpret_voice(entry["text"]), update_id, entry["context"])
                send("Голосовая команда принята: " + entry["text"][:300])
        elif data == "menu":
            show_menu(current)
        elif data in ("chats", "projects", "active", "decisions"):
            if data == "projects":
                show_cached_projects()
            elif data == "decisions":
                show_cached_decisions()
            else:
                show_cached_chats(active_only=data == "active")
        elif data.startswith("chats:page:") and data.split(":")[-1].isdecimal():
            show_cached_chats(int(data.split(":")[-1]))
        elif data.startswith("decisions:page:") and data.split(":")[-1].isdecimal():
            show_cached_decisions(int(data.split(":")[-1]))
        elif data.startswith("select:") and len(data) <= 64:
            selected_id = data.split(":", 1)[1]
            if not any(row.get("id") == selected_id for row in cached_threads()):
                send("Чат отсутствует в текущем списке. Откройте /chats ещё раз.")
                return
            current["selected_thread_id"] = selected_id
            save_state(current)
            show_cached_selection(current["selected_thread_id"])
        elif data.startswith("newproject:") and len(data) <= 64:
            current["selected_project_id"] = data.split(":", 1)[1]
            save_state(current)
            send("Проект выбран. " + settings_text(current) + " Пришлите /new и текст нового задания одним сообщением.", settings_buttons())
        elif data == "settings":
            send(settings_text(current) + " Настройки применяются к следующим действиям в Codex.", settings_buttons())
        elif data == "help":
            handle_text("/help", update_id, current)
        elif data == "models":
            show_models(current)
        elif data == "efforts":
            show_efforts(current)
        elif data.startswith("model:"):
            if not change_model(current, data.split(":", 1)[1]):
                send("Неизвестная модель. Выберите модель из меню /model.")
        elif data.startswith("effort:"):
            if not change_effort(current, data.split(":", 1)[1]):
                send("Эта интенсивность не поддерживается выбранной моделью. Откройте /effort.")
        else:
            enqueue(update_id, "callback", data, current.get("selected_thread_id"))
        return
    voice = message.get("voice")
    if voice:
        if int(voice.get("duration") or 0) > 180 or int(voice.get("file_size") or 0) > 20_000_000:
            send("Голосовое сообщение слишком длинное. Максимум 3 минуты и 20 МБ.")
            return
        try:
            transcript = transcribe_voice(str(voice["file_id"]))
            nonce = secrets.token_urlsafe(12)
            pending = {key: value for key, value in voice_pending().items() if time.time() - value.get("at", 0) <= 86400}
            pending[nonce] = {"text": transcript, "at": time.time(), "context": {
                "selected_thread_id": current.get("selected_thread_id"),
                "selected_project_id": current.get("selected_project_id"),
                "model": current.get("model"), "thinking": current.get("thinking"),
            }}
            atomic_json(VOICE_PATH, pending)
            send("Распознано: " + transcript[:3500] + "\n\nОтправить эту команду?", [[{"text": "Отправить", "callback_data": "voice_send:" + nonce}, {"text": "Отменить", "callback_data": "voice_cancel:" + nonce}]])
        except Exception:
            send("Не удалось распознать голосовое сообщение. Попробуйте ещё раз или отправьте текст.")
        return
    attachment = None
    if message.get("document"):
        item = message["document"]
        attachment = (item.get("file_id"), item.get("file_name") or "document.bin", item.get("file_size"))
    elif message.get("video"):
        item = message["video"]
        attachment = (item.get("file_id"), item.get("file_name") or "video.mp4", item.get("file_size"))
    elif message.get("photo"):
        item = max(message["photo"], key=lambda x: int(x.get("file_size") or 0))
        attachment = (item.get("file_id"), "photo.jpg", item.get("file_size"))
    if attachment:
        if not current.get("selected_thread_id"):
            send("Сначала выберите чат через /chats, затем отправьте файл ещё раз.")
            return
        cached = read_json(THREADS_CACHE).get("threads", []) if THREADS_CACHE.exists() else []
        selected = next((row for row in cached if row.get("id") == current["selected_thread_id"]), None)
        if selected and "chatgpt" in str(selected.get("kind", "")).casefold():
            send("Вложения через Desktop пока поддерживаются для локального Codex. Выберите чат Codex.")
            return
        if int(attachment[2] or 0) > 20_000_000:
            send("Файл больше 20 МБ. Отправьте меньший файл через панель.")
            return
        try:
            stored = download_attachment(str(attachment[0]), str(attachment[1]))
            attachments = [{"id": stored["id"], "name": stored["name"], "path": str((SECRET_DIR / "attachments" / (stored["id"] + Path(stored["name"]).suffix.lower()))), "mimeType": stored["mimeType"]}]
            enqueue(update_id, "followup", str(message.get("caption") or ""), current["selected_thread_id"], current, attachments=attachments)
            send("Файл принят в очередь выбранного чата Codex. Результат отправлю после обработки.")
        except Exception:
            send("Не удалось принять файл. Поддерживаются PDF, Word, Excel, PowerPoint, изображения и видео до 20 МБ.")
        return
    raw_text = message.get("text")
    reply_message = message.get("reply_to_message") or {}
    replied_to = reply_message.get("message_id")
    if isinstance(raw_text, str) and type(replied_to) is int:
        from decision_service import CUSTOM_PROMPT_PREFIX, answer, custom_reply_target
        prompt_text = reply_message.get("text")
        decision_id = custom_reply_target(sys.modules[__name__], replied_to, prompt_text)
        if decision_id:
            try:
                result = answer(sys.modules[__name__], decision_id, text=raw_text)
            except ValueError as exc:
                send("Ответ не отправлен: " + str(exc) + ". Откройте /decisions.")
            else:
                send(decision_confirmation(result))
            return
        if isinstance(prompt_text, str) and (prompt_text.startswith(CUSTOM_PROMPT_PREFIX) or
                prompt_text.startswith("Напишите свой вариант ответа на вопрос выше.")):
            send("Связь этого поля ответа с исходным вопросом не подтверждена. Откройте /decisions; сообщение не отправлялось в другой чат.")
            return
    text = str(raw_text or "").strip()
    if not text:
        send("Пришлите текстовое или голосовое сообщение.")
        return
    handle_text(text, update_id, current)


def flush_outbox() -> dict:
    result = {"sent": 0, "uncertain": 0}
    pending = sorted(OUTBOX.glob("*.bin"))
    if not pending:
        return result
    if state().get("owner_chat_id") is None:
        # No network send is possible yet. Keep every file unclaimed for the
        # first flush after pairing rather than marking it delivered.
        result["pending"] = len(pending)
        return result
    for path in pending:
        claimed = path.with_suffix(".sending")
        try:
            path.rename(claimed)
        except FileNotFoundError:
            continue  # Another CLI/service sender already owns this file.
        except FileExistsError:
            continue
        value = None
        try:
            value = read_json(claimed)
            scope_id = value.get("scopeThreadId")
            if (path.name.startswith("notice-") or scope_id is not None) and (
                    not isinstance(scope_id, str) or not thread_in_scope(scope_id)):
                # Preserve old or excluded notification data, but never send it.
                claimed.rename(path.with_suffix(".scope_blocked"))
                result["blocked"] = result.get("blocked", 0) + 1
                continue
            if state().get("owner_chat_id") is None:
                claimed.rename(path)
                result["pending"] = result.get("pending", 0) + 1
                continue
            mark_delivery(value.get("operationId"), "telegram_start")
            send(str(value.get("text") or ""), value.get("buttons"))
            mark_delivery(value.get("operationId"), "telegram_ok")
            claimed.unlink()
            result["sent"] += 1
        except OwnerNotPaired:
            # Pairing can disappear between preflight and send(). No API call
            # happened; restore the pending claim for a later safe flush.
            claimed.rename(path)
            mark_delivery(value.get("operationId") if isinstance(value, dict) else None, "outbox_queued")
            result["pending"] = result.get("pending", 0) + 1
        except Exception:
            # The network may have accepted the send before a timeout. Keep
            # the encrypted claim for diagnosis; never send it twice blindly.
            mark_delivery(value.get("operationId") if isinstance(value, dict) else None, "telegram_uncertain")
            result["uncertain"] += 1
    return result


def reply_payload(payload: dict) -> None:
    """Send a reply through the same validated, durable path as the CLI."""
    ensure_dirs()
    if (not isinstance(payload.get("text"), str) or
            len(payload["text"].encode("utf-16-le")) // 2 > 4000):
        raise ValueError("Invalid reply text")
    buttons = payload.get("buttons")
    if buttons is not None and not isinstance(buttons, list):
        raise ValueError("Invalid buttons")
    scope_id = payload.get("scopeThreadId")
    if "scopeGeneric" in payload and type(payload["scopeGeneric"]) is not bool:
        raise ValueError("Invalid generic scope")
    generic = payload.get("scopeGeneric") is True
    if generic and "scopeThreadId" in payload:
        raise ValueError("Conflicting reply scope")
    dispatch_status = payload.get("dispatch_status")
    if dispatch_status is not None and (
            dispatch_status != "uncertain" or payload.get("ok", True) is not False or not generic):
        raise ValueError("Invalid uncertain delivery marker")
    if scope_id is not None:
        require_thread_scope(scope_id)
    if state().get("owner_chat_id") is None:
        # Registration can retry its exact saved chunk after pairing. Creating
        # an outbox entry here would duplicate that later retry.
        raise OwnerNotPaired("owner_not_paired")
    operation_id = payload.get("operationId")
    mark_delivery(operation_id, "reply_cli_entered")
    if operation_id is not None:
        if not isinstance(operation_id, int) or operation_id < 0:
            raise ValueError("Invalid operation ID")
        results = read_json(RESULTS_PATH) if RESULTS_PATH.exists() else {}
        results[str(operation_id)] = {"text": payload["text"], "ok": bool(payload.get("ok", True)), "at": int(time.time()),
                                      **({"scopeThreadId": scope_id} if scope_id is not None else {}),
                                      **({"scopeGeneric": True} if generic else {}),
                                      **({"dispatch_status": dispatch_status} if dispatch_status else {})}
        if len(results) > 1000:
            results = dict(sorted(results.items(), key=lambda row: row[1]["at"])[-1000:])
        atomic_json(RESULTS_PATH, results)
        mark_delivery(operation_id, "result_cached")
    outbox_path = OUTBOX / (secrets.token_hex(8) + ".bin")
    atomic_json(outbox_path, {"text": payload["text"], "buttons": buttons, "operationId": operation_id,
                             **({"scopeThreadId": scope_id} if scope_id is not None else {}),
                             **({"scopeGeneric": True} if generic else {})})
    mark_delivery(operation_id, "outbox_queued")
    flush_outbox()
    if outbox_path.with_suffix(".scope_blocked").exists():
        raise ValueError("thread_out_of_scope")
    if outbox_path.exists() or outbox_path.with_suffix(".sending").exists():
        raise RuntimeError("delivery_unconfirmed")


def run() -> None:
    global _RESPONSE_OBSERVER, _ACTIVITY_OBSERVER
    config.validate(config.settings())
    token()
    current = state()
    # The existing user-logon task also starts the verified installed Desktop.
    # The helper reserves one attempt per Windows logon, never a chat prompt.
    try:
        from desktop_startup import run_bootstrap_once
        startup_result = run_bootstrap_once(timeout=20)
        startup_temporary = APP_DIR / "startup_status.tmp"
        startup_temporary.write_text(json.dumps(startup_result, separators=(",", ":")), encoding="utf-8")
        startup_temporary.replace(APP_DIR / "startup_status.json")
    except Exception:
        pass
    try:
        from response_watch import get_service
        _RESPONSE_OBSERVER = get_service(sys.modules[__name__])
        _RESPONSE_OBSERVER.start()
        # miniapp_server imports the canonical module when this file is the
        # executable entrypoint. Both ingress paths share one observer.
        import telegram_bridge as canonical_bridge
        canonical_bridge._RESPONSE_OBSERVER = _RESPONSE_OBSERVER
    except Exception:
        _RESPONSE_OBSERVER = None
    try:
        from activity_watch import ActivityWatch
        from miniapp_server import local_codex_rollout
        from wake_dispatcher import notify_pending
        _ACTIVITY_OBSERVER = ActivityWatch(bridge=sys.modules[__name__],
            resolve_path=local_codex_rollout, ledger_path=APP_DIR / "activity_watch.sqlite3",
            notify=notify_pending)
        _ACTIVITY_OBSERVER.sync()
        import telegram_bridge as canonical_bridge
        canonical_bridge._ACTIVITY_OBSERVER = _ACTIVITY_OBSERVER
    except Exception:
        _ACTIVITY_OBSERVER = None
        try:
            canonical_bridge._ACTIVITY_OBSERVER = None
        except (NameError, AttributeError):
            pass
    try:
        configure_telegram_commands()
    except Exception:
        pass
    from miniapp_server import serve, supervise_tunnel
    serve()
    try:
        from background_service import start_background as start_native_background
        started = start_native_background(sys.modules[__name__])
        atomic_json(APP_DIR / "background_start_status.bin", {
            "pid": os.getpid(), "started": bool(started), "at": time.time(),
        })
    except Exception as exc:
        try:
            atomic_json(APP_DIR / "background_start_status.bin", {
                "pid": os.getpid(), "started": False, "at": time.time(),
                "errorType": type(exc).__name__,
            })
        except Exception:
            pass
        # The durable queue stays intact. Never fall back to service chat prompts.
        if sys.stderr is not None:
            print("Native background bridge unavailable", file=sys.stderr, flush=True)
    try:
        from desktop_control import start_background, notify_panel_changed
        start_background()
        notify_panel_changed()
    except Exception:
        pass
    try:
        from cloud_recovery import start_background as start_cloud_recovery
        start_cloud_recovery()
    except Exception:
        # Durable commands remain queued. No service-chat fallback.
        pass
    threading.Thread(target=supervise_tunnel, daemon=True, name="TelegramTunnel").start()
    if sys.stdout is not None:
        print("Telegram bridge running", flush=True)
    while True:
        try:
            if _ACTIVITY_OBSERVER is not None:
                _ACTIVITY_OBSERVER.sync()
            voice_pending()
            flush_outbox()
            if cloud_transport_enabled():
                time.sleep(30)
                continue
            updates = api("getUpdates", {"offset": current.get("offset", 0), "timeout": 20, "allowed_updates": ["message", "callback_query"]}, timeout=30)
            # Enabling the switch during a long poll leaves returned updates
            # unacknowledged for the new transport, without advancing offset.
            if cloud_transport_enabled():
                continue
            for update in updates:
                process_update(update)
                current = state()
                current["offset"] = int(update["update_id"]) + 1
                save_state(current)
        except Exception as exc:
            if sys.stderr is not None:
                print(f"Bridge error: {type(exc).__name__}", file=sys.stderr, flush=True)
            time.sleep(30 if cloud_transport_enabled() else 5)


def save_token_and_show_pairing(value: str) -> None:
    ensure_dirs()
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", value):
        raise ValueError("Invalid bot token format")
    TOKEN_PATH.write_bytes(protect(value.encode("utf-8")))
    try:
        bot = api("getMe", {})
    except Exception:
        TOKEN_PATH.unlink(missing_ok=True)
        raise
    current = state()
    print("Bot:", bot.get("username"))
    if current.get("owner_user_id") is None:
        print("Pairing link: https://t.me/" + bot["username"] + "?start=" + current["pair_code"])
    else:
        print("Already paired to Telegram user ID", current["owner_user_id"])


def setup() -> None:
    save_token_and_show_pairing(getpass.getpass("Paste BotFather token (input hidden): ").strip())


def setup_clipboard() -> None:
    """Import a copied BotFather token without printing or shell-quoting it."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.restype = ctypes.c_void_p
    if not user32.OpenClipboard(None):
        raise ctypes.WinError()
    try:
        handle = user32.GetClipboardData(13)
        if not handle:
            raise ValueError("Clipboard has no Unicode text")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError()
        try:
            value = ctypes.wstring_at(pointer).strip()
        finally:
            kernel32.GlobalUnlock(handle)
        user32.EmptyClipboard()
    finally:
        user32.CloseClipboard()
    save_token_and_show_pairing(value)


def checkpoint(encoded: str) -> None:
    """Compare app thread snapshots without storing chat text or titles."""
    payload = json.loads(urllib.parse.unquote(encoded))
    if not isinstance(payload, list):
        raise ValueError("Expected a list of thread status records")
    initialized = WATCH_PATH.exists()
    previous = read_json(WATCH_PATH) if initialized else {}
    current = {}
    events = []
    for row in scope_rows(payload):
        thread_id = row.get("id")
        if not isinstance(thread_id, str) or len(thread_id) > 100:
            continue
        now = {"status": str(row.get("status") or "unknown"), "updatedAt": row.get("updatedAt"), "kind": str(row.get("kind") or "unknown")}
        current[thread_id] = now
        before = previous.get(thread_id)
        if not before and not initialized:
            continue
        if before is None or before["status"] != now["status"] or before["updatedAt"] != now["updatedAt"]:
            events.append({"id": thread_id, "kind": now["kind"], "before": before["status"] if before else None, "status": now["status"], "updatedAt": now["updatedAt"]})
    atomic_json(WATCH_PATH, current)
    print(json.dumps(events if state().get("owner_user_id") else [], ensure_ascii=False))


def notice(encoded: str) -> None:
    """Queue a user-facing status once per source message ID."""
    payload = json.loads(urllib.parse.unquote(encoded))
    thread_id = payload.get("threadId")
    item_id = payload.get("itemId")
    message = payload.get("text")
    if not all(isinstance(x, str) and x for x in (thread_id, item_id, message)):
        raise ValueError("threadId, itemId and text are required")
    if len(thread_id) > 100 or len(item_id) > 150 or len(message) > 4000:
        raise ValueError("Status notification is too long")
    if not thread_in_scope(thread_id):
        print('{"queued":false,"reason":"thread_out_of_scope"}')
        return
    previous = read_json(NOTICE_PATH) if NOTICE_PATH.exists() else {}
    old = previous.get(thread_id)
    seen = old.get("seen", []) if isinstance(old, dict) else ([old] if isinstance(old, str) else [])
    if item_id in seen:
        print('{"queued":false}')
        return
    atomic_json(OUTBOX / (f"notice-{time.time_ns():020d}-{secrets.token_hex(4)}.bin"),
                {"text": message, "buttons": None, "scopeThreadId": thread_id})
    previous[thread_id] = {"seen": (seen + [item_id])[-512:]}
    atomic_json(NOTICE_PATH, previous)
    print('{"queued":true}')


def main() -> None:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["serve", "setup", "setup_clipboard", "pending", "reply", "ack", "health", "checkpoint", "notice", "cache_threads", "cache_projects", "cache_archived", "cache_thread", "cache_thread_page", "cache_usage", "cache_decision", "register_decision", "webhook_status", "webhook_set", "webhook_remove", "webhook_menu", "migration_identity", "cloud_transport_on", "cloud_transport_off", "cloud_transport_status", "miniapp_status", "miniapp_probe", "activity_watch_status", "receive_desktop", "desktop_transport_status", "work_scope_status", "work_scope_register"])
    parser.add_argument("arg", nargs="?")
    parser.add_argument("--expected-host", help="Exact gateway hostname returned by Sites; webhook_set requires it")
    args = parser.parse_args()
    if args.arg == "-":
        from cli_payload import supports_stdin, read_uri_stdin
        try:
            if not supports_stdin(args.action):
                raise ValueError()
            args.arg = read_uri_stdin()
        except (Exception, KeyboardInterrupt):
            print('{"ok":false,"error":"invalid_stdin_payload"}')
            raise SystemExit(2)
    if args.action in {"work_scope_status", "work_scope_register"}:
        # Metadata-only scope CLI: no token, owner state or inbox reads.
        from work_scope import register
        if args.expected_host is not None:
            raise ValueError("invalid_scope_arguments")
        if args.action == "work_scope_status":
            if args.arg is not None:
                raise ValueError("invalid_scope_arguments")
            print(json.dumps({"ok": True, "work_thread_ids": sorted(work_scope_ids())}))
        else:
            if not args.arg:
                raise ValueError("work_scope_payload_required")
            payload = json.loads(urllib.parse.unquote(args.arg))
            created = register(APP_DIR / "work_scope", payload)
            print(json.dumps({"ok": True, "registered": created}))
        return
    if args.action in {"miniapp_probe", "activity_watch_status"}:
        try:
            coverage_id = args.arg if args.action == "activity_watch_status" else None
            if coverage_id is not None and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", coverage_id) is None:
                raise ValueError()
            if args.action == "activity_watch_status" and coverage_id is None:
                raise ValueError()
            probe_threads = json.loads(urllib.parse.unquote(args.arg)) if args.action == "miniapp_probe" and args.arg is not None else None
            probe_refresh_usage = isinstance(probe_threads, dict) and probe_threads == {"refreshUsage": True}
            if probe_refresh_usage:
                probe_threads = None
            if probe_threads is not None and (not isinstance(probe_threads, list) or len(probe_threads) > 3 or
                    any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", item) for item in probe_threads)):
                raise ValueError()
            if args.expected_host is not None:
                raise ValueError()
        except Exception:
            print('{"ok":false,"error":"invalid_input"}')
            raise SystemExit(2)
        from miniapp_probe import run_cli
        raise SystemExit(run_cli(state_provider=lambda: read_json(STATE_PATH) if STATE_PATH.is_file() else {},
                                 token_provider=token, username=EXPECTED_USERNAME, thread_ids=probe_threads,
                                 refresh_usage=probe_refresh_usage, coverage_thread_id=coverage_id))
    if args.action in {"receive_desktop", "desktop_transport_status"}:
        if args.expected_host is not None:
            print('{"ok":false,"error":"invalid_input"}')
            raise SystemExit(2)
        from desktop_transport import run_cli
        raise SystemExit(run_cli(args.action, encoded=args.arg,
            ledger_path=SECRET_DIR / "desktop_receipts.sqlite3",
            state_provider=lambda: read_json(STATE_PATH) if STATE_PATH.is_file() else {},
            process_update=lambda update: process_update(update, acknowledge_callback=False),
            expected_username=EXPECTED_USERNAME))
    if args.action == "miniapp_status":
        if args.arg is not None or args.expected_host is not None:
            print('{"ok":false,"error":"invalid_input"}')
            raise SystemExit(2)
        try:
            from desktop_transport import miniapp_status
            result = miniapp_status(lambda: read_json(APP_DIR / "miniapp_status.bin")
                                    if (APP_DIR / "miniapp_status.bin").is_file() else {})
            print(json.dumps(result, separators=(",", ":")))
            raise SystemExit(0)
        except (Exception, KeyboardInterrupt):
            print('{"ok":false,"error":"miniapp_status_unavailable"}')
            raise SystemExit(2)
    if args.action.startswith("cloud_transport_"):
        # This dispatch must stay before ensure_dirs(), token/state/queue access,
        # and the generic exception logger. The marker contains no secrets.
        if args.arg is not None or args.expected_host is not None:
            print('{"ok":false,"error":"invalid_input"}')
            raise SystemExit(2)
        from cloud_transport import run_cli
        raise SystemExit(run_cli(args.action))
    if args.action.startswith("webhook_") or args.action == "migration_identity":
        # Keep secret-bearing operations out of the generic exception logger,
        # state initialization, queue access and local service startup paths.
        try:
            from telegram_webhook_cli import run_webhook_cli, run_migration_identity, paired_menu_chat
            if args.arg is not None:
                print('{"ok":false,"error":"invalid_input"}')
                raise SystemExit(2)
            if args.action == "migration_identity":
                if args.expected_host is not None:
                    print('{"paired":false}')
                    raise SystemExit(2)
                # Read only an existing pairing through the internal decoder.
                # state() would initialize files/directories when absent.
                code = run_migration_identity(lambda: read_json(STATE_PATH) if STATE_PATH.is_file() else {})
            else:
                code = run_webhook_cli(args.action, token_provider=token, expected_host=args.expected_host,
                                       menu_chat_provider=lambda: paired_menu_chat(lambda: read_json(STATE_PATH) if STATE_PATH.is_file() else {}))
        except (Exception, KeyboardInterrupt):
            print('{"ok":false,"error":"webhook_operation_failed"}')
            code = 2
        raise SystemExit(code)
    ensure_dirs()
    if args.action == "serve":
        run()
    elif args.action == "setup":
        setup()
    elif args.action == "setup_clipboard":
        setup_clipboard()
    elif args.action == "pending":
        # Recover unpublished staging after a crash. A ledger entry prevents a
        # second CLI attempt even when the first process lost its result.
        for staged in sorted(INBOX.glob("*.fast_pending"))[:3]:
            finish_fast_pending(staged)
        print(json.dumps([read_json(p) for p in sorted(INBOX.glob("*.bin"))], ensure_ascii=False))
    elif args.action == "reply":
        if not args.arg:
            raise ValueError("URI-encoded JSON payload required")
        payload = json.loads(urllib.parse.unquote(args.arg))
        reply_payload(payload)
    elif args.action == "ack":
        if not args.arg or not args.arg.isdecimal():
            raise ValueError("Numeric update ID required")
        (INBOX / (args.arg + ".bin")).unlink(missing_ok=True)
    elif args.action == "checkpoint":
        if not args.arg:
            raise ValueError("URI-encoded thread snapshot required")
        checkpoint(args.arg)
    elif args.action == "cache_usage":
        if not args.arg:
            raise ValueError("URI-encoded usage payload required")
        cache_usage(args.arg)
    elif args.action == "cache_decision":
        if not args.arg:
            raise ValueError("URI-encoded decision payload required")
        cache_decision(args.arg)
    elif args.action == "register_decision":
        if not args.arg:
            raise ValueError("URI-encoded decision payload required")
        register_decision(args.arg)
    elif args.action == "notice":
        if not args.arg:
            raise ValueError("URI-encoded status notification required")
        notice(args.arg)
        flush_outbox()
    elif args.action in ("cache_threads", "cache_projects", "cache_archived", "cache_thread", "cache_thread_page"):
        if not args.arg:
            raise ValueError("URI-encoded cache payload required")
        cache_snapshot(args.action, args.arg)
    else:
        current = state()
        print(json.dumps({"token_configured": TOKEN_PATH.exists() or bool(os.environ.get("CTD_BRIDGE_BOT_TOKEN")), "paired": current.get("owner_user_id") is not None, "pending": len(list(INBOX.glob("*.bin"))), "outbox": len(list(OUTBOX.glob("*.bin"))), "outbox_uncertain": len(list(OUTBOX.glob("*.sending")))}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        ensure_dirs()
        (APP_DIR / "startup_error.txt").write_text(type(exc).__name__ + ": " + str(exc)[:500] + "", encoding="utf-8")
        raise

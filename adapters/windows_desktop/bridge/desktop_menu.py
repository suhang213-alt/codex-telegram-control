"""Immediate owner-authorized Desktop menu, backed by existing local accessors.

No model runner, polling loop, token reader or raw inbox reader lives here.
Only an explicit active/status request with stale status is handed to the
existing native worker once; ordinary menus remain immediate local reads.
The caller must authenticate the owner before passing authenticated=True.
Only IDs, request hashes, phases and timestamps are persisted in the journal.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote

MENU_COMMANDS = {"menu", "chats", "projects", "active", "select", "view", "help"}
ACTIVE = {"active", "running", "inprogress", "in_progress"}
STATUS_CACHE_SECONDS = 5
ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


def command(record):
    kind = record.get("kind")
    raw = str(record.get("text") or "").strip().removeprefix("/")
    if kind in ("command", "callback"):
        name = raw
    elif kind in MENU_COMMANDS:
        name = kind if kind not in ("select", "view") else kind + ":" + str(record.get("selected_thread_id") or raw)
    else:
        return None
    name = {"start": "menu", "status": "active"}.get(name, name)
    if name in MENU_COMMANDS - {"select", "view"}:
        return name, None
    match = re.fullmatch(r"(chats|projects|active):page:([0-9]{1,8})", name)
    if match:
        return match[1], int(match[2])
    match = re.fullmatch(r"(select|view)(?::|\s+)([A-Za-z0-9_-]{1,100})", name)
    return (match[1], match[2]) if match else None


def record_from_update(update):
    """Normalize only supported commands; never treat ordinary chat text as one."""
    callback = update.get("callback_query") or {}
    message = update.get("message") or {}
    if callback:
        record = {"id": update.get("update_id"), "kind": "callback", "text": callback.get("data")}
    elif isinstance(message.get("text"), str) and message["text"].startswith("/"):
        record = {"id": update.get("update_id"), "kind": "command", "text": message["text"]}
    else:
        return None
    return record if command(record) else None


def _button(text, callback):
    return {"text": text, "callback_data": callback} if len(callback.encode()) <= 64 else None


def _kind(row):
    return "ChatGPT" if "chatgpt" in str(row.get("kind", "")).casefold() else "Codex"


def _status(row):
    value = str(row.get("status") or "unknown")
    return {"idle": "готов", "active": "в работе", "running": "в работе", "notLoaded": "чат сейчас не открыт",
            "unknown": "текущий статус недоступен"}.get(value, value)


BACK = [[{"text": "Главное меню", "callback_data": "menu"}]]


def render(parsed, backend):
    name, argument = parsed
    if name == "menu":
        return {"text": "Панель компьютера. Выберите действие:", "buttons": [
            [{"text": "📁 Проекты", "callback_data": "projects"}, {"text": "💬 Чаты", "callback_data": "chats"}],
            [{"text": "🧠 Модели", "callback_data": "models"}, {"text": "⚙️ Интенсивность", "callback_data": "efforts"}],
            [{"text": "⏳ В работе", "callback_data": "active"}, {"text": "❗ Нужны решения", "callback_data": "decisions"}],
            [{"text": "Настройки", "callback_data": "settings"}, {"text": "Справка", "callback_data": "help"}]]}
    if name == "help":
        return {"text": "/chats — выбрать существующий чат; /projects — выбрать проект; /status — задачи в работе. "
                "После выбора чата отправьте текст или файл. /model и /effort меняют настройки следующих действий Codex. "
                "/menu — главное меню компьютера.", "buttons": BACK}
    if name in {"chats", "projects", "active"}:
        rows = backend.projects() if name == "projects" else backend.threads()
        key, prefix = ("projectId", "newproject:") if name == "projects" else ("id", "select:")
        rows = [row for row in rows if isinstance(row, dict) and isinstance(row.get(key), str)
                and ID.fullmatch(row[key]) and _button("", prefix + row[key])]
        listed_count = len(rows)
        unknown_status = any(str(row.get("status") or "unknown").casefold() == "unknown" for row in rows)
        if name == "active":
            rows = [row for row in rows if str(row.get("status") or "").casefold() in ACTIVE]
        pages = max(1, (len(rows) + 9) // 10)
        page = max(0, min(argument or 0, pages - 1))
        buttons = []
        for row in rows[page * 10:(page + 1) * 10]:
            label = str(row.get("label") or row.get("title") or "Без названия").replace("\n", " ")
            if name != "projects":
                label = _kind(row) + ": " + label
            buttons.append([_button(label[:60], prefix + row[key])])
        navigation = []
        if page:
            navigation.append(_button("← Назад", name + ":page:" + str(page - 1)))
        if page + 1 < pages:
            navigation.append(_button("Далее →", name + ":page:" + str(page + 1)))
        if navigation:
            buttons.append(navigation)
        buttons += BACK
        label = {"chats": "Чаты", "projects": "Проекты", "active": "В работе"}[name]
        text = f"{label} · {page + 1}/{pages}. Выберите запись:" if rows else (
            "Подтверждённых активных задач в текущем локальном списке нет." if name == "active" else "Локальный список пока пуст.")
        if name == "active":
            # Unknown/stale native status is missing evidence, never an idle state.
            if not listed_count:
                text = "Локальный список чатов пока пуст; текущую активность определить нельзя."
            elif unknown_status:
                unavailable = "Текущий статус части чатов недоступен."
                text = (text + "\n" + unavailable + " Список активных задач может быть неполным." if rows else
                        unavailable + " Полный список активных задач сейчас определить нельзя.")
        return {"text": text, "buttons": buttons}
    target = next((row for row in backend.threads() if row.get("id") == argument), None)
    if target is None:
        return {"text": "Чат отсутствует в текущем списке. Откройте /chats ещё раз.", "buttons": BACK, "ok": False}
    title = str(target.get("title") or "Без названия")[:150]
    if name == "select":
        backend.select(argument)
        view = _button("Последние сообщения", "view:" + argument)
        return {"text": f"Выбран чат [{_kind(target)}] {title}. Статус: {_status(target)}. "
                "Отправьте текст или файл; сообщение будет передано в этот чат.",
                "buttons": ([[view]] if view else []) + BACK}
    detail = backend.detail(target)
    if detail is None:
        return {"text": "Переписка пока недоступна в локальном кэше. Откройте этот чат в Desktop и обновите панель.", "buttons": BACK, "ok": False}
    backend.cache_detail(argument, detail)
    visible = [row for row in detail.get("messages", []) if isinstance(row, dict)
               and row.get("role") in {"user", "human", "assistant", "agent"} and isinstance(row.get("text"), str)]
    parts = [f"[{_kind(target)}] {title}\nПоследние сообщения выбранного чата:"]
    for row in visible[-4:]:
        role = "Вы" if row["role"] in {"user", "human"} else "Ассистент"
        text = row["text"]
        parts.append(role + ": " + (text[:720] + "…" if len(text) > 720 else text))
    if not visible:
        parts.append("В кэше пока нет видимых сообщений.")
    return {"text": "\n\n".join(parts)[:3900], "buttons": BACK}


class BridgeBackend:
    """Adapter using existing internal accessors and the supported CLI boundary."""
    def __init__(self, bridge=None, cli=None):
        if bridge is None:
            import telegram_bridge as bridge
        self.bridge, self.cli = bridge, cli or self._cli

    def _cli(self, action, payload):
        args = [sys.executable, str(Path(self.bridge.__file__).resolve()), action]
        data = None
        if action == "ack":
            args.append(str(payload))
        else:
            args.append("-")
            data = quote(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), safe="~")
        completed = subprocess.run(args, input=data, text=True, encoding="utf-8", cwd=str(Path(self.bridge.__file__).parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
        if completed.returncode:
            raise RuntimeError("bridge_cli_failed")

    def threads(self):
        now = int(time.time())
        rows = {row["id"]: dict(row) for row in self.bridge.cached_threads() if isinstance(row, dict) and isinstance(row.get("id"), str)}
        for row in rows.values():
            stamp = row.get("native_verified_at")
            if type(stamp) is not int or not 0 <= now - stamp <= 300:
                row["status"] = "unknown"
        for local in self.bridge.local_codex_threads():
            previous = rows.get(local.get("id"), {})
            stamp = previous.get("native_verified_at")
            merged = {**previous, **local}
            if type(stamp) is int and 0 <= now - stamp <= 300:
                merged["status"] = previous.get("status", "unknown")
            rows[local["id"]] = merged
        return sorted(rows.values(), key=lambda row: str(row.get("updatedAt") or ""), reverse=True)

    def projects(self):
        return self.bridge.cached_projects()

    def needs_fresh_active(self):
        # cache_threads also merges older local SQL rows, which may lack status.
        # Freshness belongs to the latest native snapshot, not those extra rows.
        rows = [row for row in self.bridge.cached_threads() if isinstance(row, dict)
                and row.get("native_verification") == "codex_native_tools"]
        now = time.time()
        return not rows or any(type(row.get("native_verified_at")) is not int
            or not 0 <= now - row["native_verified_at"] <= STATUS_CACHE_SECONDS for row in rows)

    def enqueue_active(self, identifier):
        self.bridge.enqueue(identifier, "active", "active")

    def pending_active(self, identifier):
        return self.bridge.pending_command_exists(identifier)

    def wake_active(self, identifier):
        from wake_dispatcher import notify_pending
        result = notify_pending(identifier)
        if not result.get("ok"):
            raise RuntimeError("status_wake_unavailable")

    def select(self, thread_id):
        current = self.bridge.state()
        current["selected_thread_id"] = thread_id
        self.bridge.save_state(current)

    def detail(self, row):
        if _kind(row) == "Codex" and row.get("hostId") in (None, "local"):
            from miniapp_server import local_history_page
            local = local_history_page(row["id"])
            if local is not None:
                return local
        path = self.bridge.THREAD_DETAILS / (row["id"] + ".bin")
        return self.bridge.read_json(path).get("detail") if path.is_file() else None

    def cache_detail(self, thread_id, detail):
        self.cli("cache_thread", {"id": thread_id, "messages": detail.get("messages", [])[-30:], "nextCursor": detail.get("nextCursor")})

    def reply(self, payload):
        self.cli("reply", payload)

    def ack(self, identifier):
        self.cli("ack", identifier)


def _defer_active(record, backend, db, phase, clock):
    """Reply once, publish one read-only pending and signal its original ID.

    Never ACK here: native worker owns the fresh result and its ACK. A crash
    after enqueue intent cannot recreate a receipt already consumed by worker.
    """
    identifier = str(record["id"])
    if phase == "refresh_woken":
        db.commit()
        return {"handled": True, "ok": True, "disposition": "deduplicated"}
    if phase == "refresh_replying":
        db.commit()
        return {"handled": True, "ok": False, "error": "reply_uncertain"}
    if phase == "preparing":
        db.execute("UPDATE menu_receipts SET phase='refresh_replying',updated_at=? WHERE id=?", (int(clock()), identifier))
        db.commit()
        backend.reply({"text": "Проверяю текущие задачи в Desktop. Пришлю свежий статус.", "buttons": BACK})
        db.execute("UPDATE menu_receipts SET phase='refresh_prepared',updated_at=? WHERE id=?", (int(clock()), identifier))
        phase = "refresh_prepared"
    elif phase == "refresh_enqueuing":
        db.commit()
        if not backend.pending_active(record["id"]):
            return {"handled": True, "ok": False, "error": "status_enqueue_uncertain"}
        db.execute("UPDATE menu_receipts SET phase='refresh_enqueued',updated_at=? WHERE id=?", (int(clock()), identifier))
        phase = "refresh_enqueued"
    else:
        db.commit()
    if phase == "refresh_prepared":
        claim = db.execute("UPDATE menu_receipts SET phase='refresh_enqueuing',updated_at=? WHERE id=? AND phase='refresh_prepared'", (int(clock()), identifier))
        if claim.rowcount != 1:
            return {"handled": True, "ok": True, "disposition": "in_progress"}
        backend.enqueue_active(record["id"])
        db.execute("UPDATE menu_receipts SET phase='refresh_enqueued',updated_at=? WHERE id=?", (int(clock()), identifier))
        phase = "refresh_enqueued"
    if phase == "refresh_enqueued":
        backend.wake_active(record["id"])
        db.execute("UPDATE menu_receipts SET phase='refresh_woken',updated_at=? WHERE id=?", (int(clock()), identifier))
    return {"handled": True, "ok": True, "disposition": "deferred"}


def handle_pending(record, *, backend, ledger_path, authenticated=False, clock=time.time):
    """Handle only supported authenticated reads; unknown commands stay native.

    replied -> ack can recover. A crash/timeout while reply was in flight is
    uncertain and must not send it again automatically.
    """
    if not authenticated:
        return {"handled": False, "ok": False, "error": "owner_authentication_required"}
    if not isinstance(record, dict) or type(record.get("id")) is not int or record["id"] < 0:
        return {"handled": False, "ok": False, "error": "invalid_command"}
    parsed = command(record)
    if parsed is None:
        return {"handled": False}
    identifier = str(record["id"])
    digest = hashlib.sha256(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    path = Path(ledger_path); path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS menu_receipts (id TEXT PRIMARY KEY,digest TEXT NOT NULL,phase TEXT NOT NULL,updated_at INTEGER NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT digest,phase FROM menu_receipts WHERE id=?", (identifier,)).fetchone()
        if old and old[0] != digest:
            db.commit(); return {"handled": True, "ok": False, "error": "command_conflict"}
        if old and old[1] == "done":
            db.commit(); return {"handled": True, "ok": True, "disposition": "deduplicated"}
        if old and old[1] == "replying":
            db.commit(); return {"handled": True, "ok": False, "error": "reply_uncertain"}
        if not old:
            db.execute("INSERT INTO menu_receipts VALUES (?,?,?,?)", (identifier, digest, "preparing", int(clock())))
        phase = old[1] if old else "preparing"
        refresh_check = getattr(backend, "needs_fresh_active", None)
        if phase.startswith("refresh_") or (phase == "preparing" and parsed[0] == "active"
                and callable(refresh_check) and refresh_check()):
            return _defer_active(record, backend, db, phase, clock)
        if not old or old[1] == "preparing":
            payload = render(parsed, backend)
            if record["id"] >= 1_000_000_000_000_000_000:
                payload.update(operationId=record["id"], ok=payload.get("ok", True))
            db.execute("UPDATE menu_receipts SET phase='replying',updated_at=? WHERE id=?", (int(clock()), identifier))
            db.commit()  # One owner claims reply before any external send.
            backend.reply(payload)
            db.execute("UPDATE menu_receipts SET phase='replied',updated_at=? WHERE id=?", (int(clock()), identifier))
        else:
            db.commit()
        backend.ack(record["id"])
        db.execute("UPDATE menu_receipts SET phase='done',updated_at=? WHERE id=?", (int(clock()), identifier))
        return {"handled": True, "ok": True, "disposition": "completed"}
    except Exception:
        return {"handled": True, "ok": False, "error": "menu_operation_failed"}
    finally:
        db.close()

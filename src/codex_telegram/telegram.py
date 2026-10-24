"""Telegram transport: one preconfigured owner, no enrollment by username."""
from __future__ import annotations
import json
import queue
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from .config import SafeError
from .security import owned_message


class Telegram:
    def __init__(self, token, owner_id, public_url, controller):
        self._token, self.owner_id, self.public_url, self.control = token, owner_id, public_url, controller
        self.stop = threading.Event()
        self.status = "not_started"
        self.last_error = None

    def call(self, method, data=None, timeout=40):
        request = urllib.request.Request("https://api.telegram.org/bot" + self._token + "/" + method,
                    data=json.dumps(data or {}).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = json.loads(response.read(22_000_000))
            if not value.get("ok"):
                raise SafeError("Telegram rejected a request. Check bot settings and connectivity.")
            return value.get("result")
        except (OSError, ValueError):
            # Never show urllib exceptions; they can contain the bot token in the URL.
            raise SafeError("Telegram request failed. No automatic resend of outgoing messages.") from None

    def send(self, text):
        # Plain text only; model output cannot inject Telegram markup or links into buttons.
        for offset in range(0, len(text), 3500):
            self.call("sendMessage", {"chat_id": self.owner_id, "text": text[offset:offset + 3500],
                                      "link_preview_options": {"is_disabled": True}})

    def send_document(self, name, content):
        if not re.fullmatch(r"[^\r\n\"\\/]{1,120}", name):
            raise SafeError("Unsafe export name.")
        boundary = "----ctc" + secrets.token_hex(16)
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{self.owner_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{name}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request("https://api.telegram.org/bot" + self._token + "/sendDocument", data=body,
                                         headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read(100000))
            if not result.get("ok"):
                raise ValueError()
        except (OSError, ValueError):
            raise SafeError("File delivery is unconfirmed. Check Telegram before retrying.") from None

    def configure_menu(self):
        self.call("setChatMenuButton", {"chat_id": self.owner_id, "menu_button": {"type": "web_app", "text": "Codex", "web_app": {"url": self.public_url}}})
        self.call("setMyCommands", {"scope": {"type": "chat", "chat_id": self.owner_id}, "commands": [
            {"command": "panel", "description": "Open control panel"},
            {"command": "new", "description": "Create a chat: /new title"},
            {"command": "chats", "description": "List this installation's chats"},
            {"command": "use", "description": "Select a chat: /use number"},
            {"command": "status", "description": "Connection and task status"},
            {"command": "stop", "description": "Interrupt selected task"},
            {"command": "files", "description": "List explicitly exported files"},
            {"command": "get", "description": "Receive an exported file: /get name"},
            {"command": "help", "description": "Instructions"}]})

    def prepare(self):
        self.call("getMe")
        webhook = self.call("getWebhookInfo")
        if webhook.get("url"):
            raise SafeError("This bot has a webhook. Use a separate bot or explicitly remove its webhook first.")
        self.configure_menu()

    def handle(self, update):
        message = update.get("message", {})
        if not owned_message(message, self.owner_id):
            return
        text = message.get("text", "")
        if not isinstance(text, str):
            return
        if not text:
            self.send("Use text or upload an attachment through the panel. Voice transcription is not enabled in the portable core.")
            return
        request_id = "telegram_" + str(update["update_id"])
        command, _, arg = text.partition(" ")
        command = command.split("@", 1)[0]
        if command in ("/start", "/help"):
            if self.control.config.get("backend")=="desktop":
                self.send("Codex Desktop на вашем Mac. /chats — существующие чаты разрешённой папки; /use номер — выбрать чат. Обычный текст продолжает этот же чат. /panel — панель и разрешения; /stop — остановить задачу; /files и /get имя — файлы из telegram-exports. Новый чат создавайте в Desktop. Mac, Desktop и мост должны оставаться включёнными.")
            else:
                self.send("Private local Codex. /new title creates an isolated chat; /chats lists chats; /use number selects one. Send text to the selected chat. /panel opens settings and explicit approvals. /stop interrupts a task. /files and /get name return files placed in telegram-exports. The computer and bridge must remain running.")
        elif command == "/panel":
            self.call("sendMessage", {"chat_id": self.owner_id, "text": "Open your private control panel:", "reply_markup": {"inline_keyboard": [[{"text": "Codex", "web_app": {"url": self.public_url}}]]}})
        elif command == "/new":
            if self.control.config.get("backend")=="desktop":
                self.send("Создайте новый чат в Codex Desktop на Mac, затем обновите /chats.")
                return
            self.control.execute(request_id, "new", {"title": arg.strip() or "New chat"})
            self.send("Chat created and selected. Send your task as the next message.")
        elif command == "/chats":
            rows = self.control.snapshot()["threads"]
            self.send("\n".join(f"{i}. {x['title']} · {x['status']}" for i, x in enumerate(rows, 1)) or "No chats yet. Use /new title.")
        elif command == "/use":
            rows = self.control.snapshot()["threads"]
            try:
                index = int(arg) - 1
                if index < 0:
                    raise ValueError()
                identifier = rows[index]["id"]
            except (ValueError, IndexError):
                raise SafeError("Choose a chat number from /chats.") from None
            self.control.execute(request_id, "select", {"thread_id": identifier})
            self.send("Chat selected.")
        elif command == "/status":
            snap = self.control.snapshot()
            self.send(f"Codex connected: {snap['connected']}. Mode: {snap['sandbox']}. Operations with uncertain outcome: {snap['uncertain_operations']}. Open the panel for the selected chat's details.")
        elif command == "/stop":
            self.control.execute(request_id, "stop", {})
            self.send("Interruption requested; completion will be confirmed by Codex.")
        elif command == "/files":
            self.send("\n".join(f"{x['name']} ({x['size']} bytes)" for x in self.control.files()) or "No files in telegram-exports.")
        elif command == "/get":
            self.send_document(arg, self.control.download(arg))
        elif text.startswith("/"):
            self.send("Unknown command. Use /help.")
        else:
            self.control.execute(request_id, "send", {"text": text})
            self.send("Message accepted by Codex. This confirms receipt, not completion.")

    def poll(self):
        self.status = "running"
        while not self.stop.is_set():
            try:
                with self.control.lock:
                    offset = self.control.data["offset"]
                updates = self.call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]})
                for update in updates:
                    if self.stop.is_set():
                        break
                    update_id = update.get("update_id")
                    if type(update_id) is not int or update_id < offset:
                        continue
                    try:
                        self.handle(update)
                    except SafeError as exc:
                        self.last_error = str(exc)
                        self.send(str(exc))
                    finally:
                        # Advance only after handling. Mutations reserve their durable
                        # request ID before RPC; a crash cannot silently skip an unseen task.
                        with self.control.lock:
                            self.control.data["offset"] = update_id + 1
                            self.control._save()
                self.status = "running"
            except (SafeError, OSError):
                self.status = "connection_error"
                self.last_error = "Telegram connection needs attention. Outgoing delivery is not automatically retried."
                self.stop.wait(5)

    def notify(self):
        while not self.stop.is_set():
            try:
                text = self.control.notifications.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.send(text)
            except SafeError:
                self.last_error = "Notification delivery unconfirmed; results remain in Codex and the panel."

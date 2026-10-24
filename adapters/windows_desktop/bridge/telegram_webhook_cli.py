"""Narrow webhook administration. No state, queue, token export or logging.

webhook_set --expected-host <host returned by Sites get_site>
  Hidden/non-TTY stdin: exactly {"url": "https://<host>/telegram", "secret": "..."}.
webhook_status [--expected-host <verified host>]
webhook_remove
webhook_menu
migration_identity

The expected hostname is a separate operator-approved CLI argument, never data
from a Telegram update. Cloud gateway enrollment is intentionally unavailable
in this public Desktop adapter; ALLOWED_GATEWAY_HOSTS is empty.
This module does not read secrets; token_provider is called only
after input validation and its value is used in memory for Telegram requests.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

MAX_INPUT_BYTES = 8192
MAX_RESPONSE_BYTES = 65536
ALLOWED_GATEWAY_HOSTS = frozenset()
CLOUD_PANEL_URL = ""
BOT_COMMANDS = [
    {"command": "menu", "description": "Выбрать панель"},
    {"command": "projects", "description": "Локальные проекты и новые задачи"},
    {"command": "chats", "description": "Все локальные чаты"},
    {"command": "status", "description": "Локальные запросы в работе"},
    {"command": "decisions", "description": "Чаты, где нужно ваше решение"},
    {"command": "model", "description": "Выбрать модель Codex"},
    {"command": "effort", "description": "Интенсивность модели"},
    {"command": "help", "description": "Справка"},
]
ERROR_CODES = frozenset({
    "invalid_expected_host", "hidden_stdin_required", "invalid_input", "gateway_url_mismatch",
    "invalid_webhook_secret", "invalid_operation", "telegram_redirect_rejected", "telegram_http_error",
    "invalid_telegram_response", "telegram_request_failed", "expected_host_required", "token_unavailable",
    "webhook_verification_failed", "webhook_operation_failed", "menu_verification_failed", "menu_pairing_unavailable",
})


class WebhookCliError(Exception):
    def __init__(self, code: str):
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WebhookCliError("telegram_redirect_rejected")


def expected_url(host):
    if not isinstance(host, str) or len(host) > 253 or host != host.lower() or host not in ALLOWED_GATEWAY_HOSTS:
        raise WebhookCliError("invalid_expected_host")
    labels = host.split(".")
    if len(labels) < 3 or labels[-2:] != ["chatgpt", "site"]:
        raise WebhookCliError("invalid_expected_host")
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise WebhookCliError("invalid_expected_host")
    return "https://" + host + "/telegram"


def read_configuration(stream, approved_url):
    if hasattr(stream, "isatty") and stream.isatty():
        raise WebhookCliError("hidden_stdin_required")
    raw = stream.read(MAX_INPUT_BYTES + 1)
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        raise WebhookCliError("invalid_input")
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise WebhookCliError("invalid_input")
                result[key] = value
            return result
        payload = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        raise WebhookCliError("invalid_input") from None
    if not isinstance(payload, dict) or set(payload) != {"url", "secret"}:
        raise WebhookCliError("invalid_input")
    if payload["url"] != approved_url:
        raise WebhookCliError("gateway_url_mismatch")
    if not isinstance(payload["secret"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", payload["secret"]):
        raise WebhookCliError("invalid_webhook_secret")
    return payload


def telegram_request(token_value, method, payload, open_request):
    if method not in {"getWebhookInfo", "setWebhook", "deleteWebhook", "setChatMenuButton", "getChatMenuButton", "setMyCommands", "getMyCommands"}:
        raise WebhookCliError("invalid_operation")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token_value + "/" + method,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with open_request(request, timeout=20) as response:
            if response.status != 200:
                raise WebhookCliError("telegram_http_error")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise WebhookCliError("telegram_redirect_rejected" if 300 <= error.code < 400 else "telegram_http_error") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise WebhookCliError("invalid_telegram_response")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        raise WebhookCliError("invalid_telegram_response") from None
    if not isinstance(value, dict) or value.get("ok") is not True or "result" not in value:
        raise WebhookCliError("telegram_request_failed")
    return value["result"]


def safe_status(info, approved_url=None, *, include_diagnostics=True):
    if not isinstance(info, dict) or not isinstance(info.get("url"), str):
        raise WebhookCliError("invalid_telegram_response")
    count = info.get("pending_update_count")
    if type(count) is not int or count < 0:
        raise WebhookCliError("invalid_telegram_response")
    status = {"ok": True, "webhook_configured": bool(info["url"]), "pending_update_count": count}
    if approved_url is not None:
        status["matches_expected"] = info["url"] == approved_url
    if include_diagnostics:
        raw_date = info.get("last_error_date")
        date = raw_date if type(raw_date) is int and 0 <= raw_date <= 253402300799 else 0
        message = info.get("last_error_message")
        message = message[:4096] if isinstance(message, str) else ""
        http_status = None
        # Extract only a numeric HTTP status from known delivery diagnostics.
        # Never return the original message, URL, header or token-shaped text.
        for pattern in (
            r"\bWrong\s+response\b[^\r\n]{0,160}?:\s*([1-5][0-9]{2})(?=\s|$)",
            r"\bHTTP(?:/[0-9](?:\.[0-9])?)?(?:\s+(?:status(?:\s+code)?|error))?\s*[:=]?\s*([1-5][0-9]{2})(?=\s|$)",
        ):
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                http_status = int(match.group(1))
                break
        status.update({"last_error_date": date, "last_error_present": bool(message.strip()) or date > 0, "last_delivery_http_status": http_status})
    return status


def run_webhook_cli(action, *, token_provider, expected_host=None, stdin=None, stdout=None, open_request=None, menu_chat_provider=None):
    """Return exit code and emit only a whitelisted, non-sensitive JSON result.

    Tests inject fake token_provider and HTTP transport. Production transport
    disables redirects; the existing DPAPI accessor never returns to stdout.
    No retries are made after uncertain mutations: inspect webhook_status.
    """
    source = stdin if stdin is not None else sys.stdin
    output = stdout if stdout is not None else sys.stdout
    try:
        if action not in {"webhook_status", "webhook_set", "webhook_remove", "webhook_menu"}:
            raise WebhookCliError("invalid_operation")
        approved_url = expected_url(expected_host) if expected_host is not None else None
        if action == "webhook_set" and approved_url is None:
            raise WebhookCliError("expected_host_required")
        if action in {"webhook_remove", "webhook_menu"} and expected_host is not None:
            raise WebhookCliError("invalid_input")
        configuration = read_configuration(source, approved_url) if action == "webhook_set" else None
        menu_chat_id = None
        if action == "webhook_menu":
            if not callable(menu_chat_provider):
                raise WebhookCliError("menu_pairing_unavailable")
            menu_chat_id = menu_chat_provider()
            if type(menu_chat_id) is not int or menu_chat_id <= 0 or menu_chat_id > 9007199254740991:
                raise WebhookCliError("menu_pairing_unavailable")
        token_value = token_provider()
        if not isinstance(token_value, str) or not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", token_value):
            raise WebhookCliError("token_unavailable")
        transport = open_request or urllib.request.build_opener(NoRedirect()).open
        if action == "webhook_menu":
            # The tunnel alone owns the paired chat's permanent Desktop
            # button. Configure the global fallback and /panels shortcuts
            # without resetting that owner's explicit Web App override.
            if telegram_request(token_value, "setChatMenuButton", {"menu_button": {"type": "commands"}}, transport) is not True:
                raise WebhookCliError("telegram_request_failed")
            commands_scope = {"type": "chat", "chat_id": menu_chat_id}
            if telegram_request(token_value, "setMyCommands", {"scope": commands_scope, "commands": BOT_COMMANDS}, transport) is not True:
                raise WebhookCliError("telegram_request_failed")
            actual = telegram_request(token_value, "getChatMenuButton", {}, transport)
            if not isinstance(actual, dict) or actual.get("type") != "commands":
                raise WebhookCliError("menu_verification_failed")
            if telegram_request(token_value, "getMyCommands", {"scope": commands_scope}, transport) != BOT_COMMANDS:
                raise WebhookCliError("menu_verification_failed")
            print('{"ok":true,"menu_configured":true}', file=output)
            return 0
        if action == "webhook_set":
            result = telegram_request(token_value, "setWebhook", {"url": approved_url, "secret_token": configuration["secret"], "allowed_updates": ["message", "callback_query"], "max_connections": 1, "drop_pending_updates": False}, transport)
            if result is not True:
                raise WebhookCliError("telegram_request_failed")
        elif action == "webhook_remove":
            if telegram_request(token_value, "deleteWebhook", {"drop_pending_updates": False}, transport) is not True:
                raise WebhookCliError("telegram_request_failed")
        info = telegram_request(token_value, "getWebhookInfo", {}, transport)
        # Preserve the minimal set/remove acknowledgement used by the hidden
        # launcher; delivery diagnostics are a read-only status operation.
        status = safe_status(info, approved_url, include_diagnostics=action == "webhook_status")
        if action == "webhook_set" and not status["matches_expected"]:
            raise WebhookCliError("webhook_verification_failed")
        if action == "webhook_remove" and status["webhook_configured"]:
            raise WebhookCliError("webhook_verification_failed")
        print(json.dumps(status, separators=(",", ":")), file=output)
        return 0
    except WebhookCliError as error:
        result = {"ok": False, "error": error.code if error.code in ERROR_CODES else "webhook_operation_failed"}
    except (Exception, KeyboardInterrupt):
        # Never stringify token-provider, transport, JSON or TLS exceptions.
        result = {"ok": False, "error": "webhook_operation_failed"}
    print(json.dumps(result, separators=(",", ":")), file=output)
    return 2


def paired_menu_chat(state_provider):
    """Resolve one already paired private chat through an internal reader.

    This does not initialize state or disclose the ID. run_webhook_cli invokes
    its injected provider only for webhook_menu, before reading a bot token.
    """
    current = state_provider()
    if not isinstance(current, dict):
        raise WebhookCliError("menu_pairing_unavailable")
    user_id, chat_id = current.get("owner_user_id"), current.get("owner_chat_id")
    if any(type(value) is not int or value <= 0 or value > 9007199254740991 for value in (user_id, chat_id)) or user_id != chat_id:
        raise WebhookCliError("menu_pairing_unavailable")
    return chat_id


def run_migration_identity(state_provider, *, stdout=None):
    """Read existing pairing through an injected internal reader; never token().

    Missing pairing emits paired:false/exit 0. Invalid/unreadable pairing emits
    paired:false/exit 2. No names, username, token, queue data or diagnostics.
    """
    output = stdout if stdout is not None else sys.stdout
    result, code = {"paired": False}, 0
    try:
        current = state_provider()
        if not isinstance(current, dict):
            raise ValueError()
        user_id, chat_id = current.get("owner_user_id"), current.get("owner_chat_id")
        if user_id is not None or chat_id is not None:
            if any(type(value) is not int or value <= 0 or value > 9007199254740991 for value in (user_id, chat_id)) or user_id != chat_id:
                raise ValueError()
            result = {"paired": True, "user_id": user_id, "chat_id": chat_id}
    except (Exception, KeyboardInterrupt):
        code = 2
    print(json.dumps(result, separators=(",", ":")), file=output)
    return code

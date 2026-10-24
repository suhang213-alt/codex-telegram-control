"""Explicit read-only loopback probe; never prints auth, paths or chat text."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8787"
MAX_RESPONSE = 5_000_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect_rejected")


def probe(*, state_provider, token_provider, username, open_request=None, now=None, thread_ids=None, refresh_usage=False, coverage_thread_id=None):
    if coverage_thread_id is not None and (not isinstance(coverage_thread_id, str) or
            re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", coverage_thread_id) is None):
        raise ValueError("invalid_coverage_thread_id")
    if thread_ids is not None and (not isinstance(thread_ids, list) or len(thread_ids) > 3 or
            any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", item) for item in thread_ids)):
        raise ValueError("invalid_thread_ids")
    current = state_provider()
    owner = current.get("owner_user_id") if isinstance(current, dict) else None
    if type(owner) is not int or owner <= 0 or owner != current.get("owner_chat_id"):
        raise ValueError("pairing_unavailable")
    fields = {"auth_date": str(int(time.time() if now is None else now)), "query_id": "readonly-local-probe",
              "user": json.dumps({"id": owner, "username": username})}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    token = token_provider()
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    init_data = urllib.parse.urlencode(fields)
    transport = open_request or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open
    metrics = {}

    def get(path, authorized=True, *, method="GET"):
        started = time.monotonic()
        headers = {"X-Telegram-Init-Data": init_data} if authorized else {}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(BASE + path, method=method, headers=headers,
                                         data=b"{}" if method == "POST" else None)
        try:
            with transport(request, timeout=20 if method == "POST" else 10) as response:
                raw = response.read(MAX_RESPONSE + 1)
                metrics[path] = {"bytes": len(raw), "elapsedMs": round((time.monotonic() - started) * 1000)}
                if len(raw) > MAX_RESPONSE:
                    raise ValueError("response_too_large")
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as error:
            # Do not read/log remote error bodies or expose auth diagnostics.
            return error.code, {}

    if coverage_thread_id is not None:
        path = "/api/worker/coverage/" + coverage_thread_id
        unauthorized, _ = get(path, False)
        status, value = get(path)
        valid = (status == 200 and isinstance(value, dict) and
                 type(value.get("alive")) is bool and type(value.get("covered")) is bool)
        return {"ok": valid and unauthorized == 401,
                "alive": value.get("alive") if valid else False,
                "covered": value.get("covered") if valid else False}

    unauthorized, _ = get("/api/threads", False)
    checks = {"unauthorizedRejected": unauthorized == 401}
    counts = {}
    for path, field, label in (("/api/threads", "threads", "threads"),
                               ("/api/projects", "projects", "projects"),
                               ("/api/archived", "threads", "archived")):
        status, value = get(path)
        rows = value.get(field) if isinstance(value, dict) else None
        valid = status == 200 and isinstance(rows, list)
        checks[label] = valid
        counts[label] = len(rows) if valid else None
    status, value = get("/api/usage")
    checks["usage"] = status == 200 and isinstance(value, dict) and "usage" in value
    result = {"ok": all(checks.values()), "checks": checks, "counts": counts,
              "usageAvailable": checks["usage"] and value.get("usage") is not None}
    if refresh_usage:
        blocked, _ = get("/api/usage/refresh", False, method="POST")
        status, refreshed = get("/api/usage/refresh", method="POST")
        usage = refreshed.get("usage") or {}
        primary = usage.get("primary") or {}
        allowed_usage = {"limitId", "primary", "secondary", "updated_at"}
        allowed_window = {"usedPercent", "windowDurationMins", "resetsAt"}
        sanitized = (set(usage) <= allowed_usage and all(
            window is None or isinstance(window, dict) and set(window) <= allowed_window
            for window in (usage.get("primary"), usage.get("secondary"))))
        stamp = usage.get("updated_at")
        recent = type(stamp) in (int, float) and 0 <= time.time() - stamp < 5
        valid = (status == 200 and (refreshed.get("refreshed") is True or recent) and
                 refreshed.get("refreshError") is None and sanitized)
        used = primary.get("usedPercent")
        result["usageRefresh"] = {
            "ok": valid, "unauthorizedRejected": blocked == 401, "safeFieldsOnly": sanitized,
            "updatedAt": usage.get("updated_at"), "stale": refreshed.get("stale"),
            "fetchedNow": refreshed.get("refreshed") is True,
            "remainingPercent": 100 - used if type(used) in (int, float) and 0 <= used <= 100 else None,
            "windowDurationMins": primary.get("windowDurationMins"),
            "secondaryAvailable": usage.get("secondary") is not None,
            **metrics.get("/api/usage/refresh", {}),
        }
        result["ok"] = result["ok"] and valid and blocked == 401
    if thread_ids:
        details = []
        for thread_id in thread_ids:
            path = "/api/threads/" + thread_id
            try:
                status, value = get(path)
                messages = value.get("messages") if isinstance(value, dict) else None
                details.append({"id": thread_id, "status": status, "messages": len(messages) if isinstance(messages, list) else None,
                                **metrics.get(path, {})})
            except Exception as error:
                details.append({"id": thread_id, "error": type(error).__name__, **metrics.get(path, {})})
        result["details"] = details
        result["ok"] = result["ok"] and all(row.get("status") == 200 and row.get("messages") is not None for row in details)
    return result


def run_cli(*, state_provider, token_provider, username, stdout=None, open_request=None, thread_ids=None, refresh_usage=False, coverage_thread_id=None):
    output = sys.stdout if stdout is None else stdout
    try:
        result = probe(state_provider=state_provider, token_provider=token_provider,
                       username=username, open_request=open_request, thread_ids=thread_ids,
                       refresh_usage=refresh_usage, coverage_thread_id=coverage_thread_id)
    except (Exception, KeyboardInterrupt):
        result = {"ok": False, "error": "miniapp_probe_failed"}
    print(json.dumps(result, separators=(",", ":")), file=output)
    return 0 if result["ok"] else 2

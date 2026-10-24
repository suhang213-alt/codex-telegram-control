"""Wake an already queued local chat through the installed Desktop protocol.

Verified in OpenAI.Codex 26.1013.4894.0 (Windows packaged app):
AppxManifest.xml registers codex with --single-argument %1.
app.asar/.vite/build/bootstrap-cdWF4rWM.js parses threads/wake/<UUID>,
no query/hash, separately from navigation (wakeQueuedThread).
main-C4DYk222.js wakes using the Desktop's getLocalAppServerConnection,
thread/queue/list limit=1 and resumeThread only for a nonempty queue.

This function adds NO prompt and does NOT assert that execution completed.
The caller must observe the matching turn/result separately. No subprocess,
config change, permission override, injected-tool imitation, or polling.
"""
from __future__ import annotations

import os
import re

THREAD_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class QueuedWakeFailure(Exception):
    """Queue is durably accepted; only OS wake delivery is unconfirmed."""


def wake_uri(thread_id):
    if not isinstance(thread_id, str) or THREAD_ID.fullmatch(thread_id) is None:
        raise ValueError("invalid_thread_id")
    return "codex://threads/wake/" + thread_id.lower()


def wake_thread(thread_id, *, opener=None):
    """Call after durable queue acceptance; never enqueue or resend text."""
    try:
        uri = wake_uri(thread_id)
    except ValueError:
        return {"ok": False, "error": "invalid_thread_id"}
    if opener is None:
        opener = getattr(os, "startfile", None)
    if not callable(opener):
        return {"ok": False, "error": "desktop_protocol_unavailable"}
    try:
        opener(uri)
    except Exception:
        # OS error messages may contain executable paths or private arguments.
        return {"ok": False, "error": "desktop_wake_unconfirmed"}
    return {"ok": True, "wakeRequested": True, "executionConfirmed": False}

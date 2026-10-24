"""Bounded hidden stdin for URI-encoded bridge payloads; never logs input."""
from __future__ import annotations

import re
import sys
import urllib.parse

MAX_DECODED_BYTES = 1024 * 1024
MAX_ENCODED_BYTES = 10 * MAX_DECODED_BYTES


def supports_stdin(action):
    return action.startswith("cache_") or action in {"reply", "receive_desktop", "checkpoint", "notice"}


def read_uri_stdin(stream=None):
    source = sys.stdin if stream is None else stream
    if hasattr(source, "isatty") and source.isatty():
        raise ValueError("hidden_stdin_required")
    raw = source.read(MAX_ENCODED_BYTES + 1)
    if not isinstance(raw, str) or len(raw) > MAX_ENCODED_BYTES or not raw.isascii():
        raise ValueError("invalid_stdin_payload")
    encoded = raw.strip()
    if not encoded or re.search(r"%(?![A-Fa-f0-9]{2})", encoded):
        raise ValueError("invalid_stdin_payload")
    decoded = urllib.parse.unquote_to_bytes(encoded)
    if len(decoded) > MAX_DECODED_BYTES:
        raise ValueError("stdin_payload_too_large")
    decoded.decode("utf-8", errors="strict")
    return encoded

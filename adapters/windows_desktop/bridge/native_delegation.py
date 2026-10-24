"""Correlate the verified Desktop representation of delegated owner input.

The installed native read_thread exposes send_message_to_thread input as a
functionCallOutput, not necessarily a userMessage. This helper accepts only that
specific, complete native envelope. It returns raw item indices, never output
contents; callers must not publish tool output as conversation text.

The caller still owns thread/turn and baseline verification, the digest of the
actual submitted prompt, and ordering of the response after the matched input.
Recognizing a matching input alone does not prove that the operation completed.
"""
from __future__ import annotations

import hashlib
import hmac
import re


_THREAD_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ITEM_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_NATIVE_ENTITY = re.compile(r"&(amp|lt|gt);")
_NATIVE_ENTITY_TEXT = {"amp": "&", "lt": "<", "gt": ">"}
_SUFFIX = "</input>\n</codex_delegation>"


def matched_delegated_input_indices(
    items: list | tuple, *, source_thread_id: str, prompt_digest: str
) -> list[int]:
    """Return raw indices matching the exact authorized source and input hash.

    Missing or truncated output is insufficient evidence and yields no match.
    The source envelope is deliberately matched byte-for-byte. Desktop escapes
    &, <, > in that order; decode those three entities once and validate the
    canonical round trip. XML parsing would normalize line endings or expand
    unrelated entities in the original prompt.
    """
    if (
        not isinstance(source_thread_id, str)
        or _THREAD_ID.fullmatch(source_thread_id) is None
        or not isinstance(prompt_digest, str)
        or _DIGEST.fullmatch(prompt_digest) is None
        or not isinstance(items, (list, tuple))
    ):
        raise ValueError("invalid_delegation_correlation")
    prefix = (
        "<codex_delegation>\n"
        f"  <source_thread_id>{source_thread_id}</source_thread_id>\n"
        "  <input>"
    )
    result = []
    for index, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or item.get("type") != "functionCallOutput"
            or item.get("name") != "send_message_to_thread"
            or item.get("namespace") != "codex_app"
            or not isinstance(item.get("id"), str)
            or _ITEM_ID.fullmatch(item["id"]) is None
        ):
            continue
        output = item.get("output")
        if not isinstance(output, dict) or output.get("truncated") is not False:
            continue
        text = output.get("text")
        if (
            not isinstance(text, str)
            or not text.startswith(prefix)
            or not text.endswith(_SUFFIX)
            or len(text) < len(prefix) + len(_SUFFIX)
        ):
            continue
        wire_prompt = text[len(prefix):-len(_SUFFIX)]
        prompt = _NATIVE_ENTITY.sub(
            lambda match: _NATIVE_ENTITY_TEXT[match.group(1)], wire_prompt
        )
        canonical = prompt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if canonical != wire_prompt:
            continue
        actual_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if hmac.compare_digest(actual_digest, prompt_digest):
            result.append(index)
    return result

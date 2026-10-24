"""Read the complete native history for user-visible result references only.

The ordinary chat cache intentionally contains only recent messages. Artifact
discovery must not inherit that limit. Raw command/tool text is never parsed as
an output file; explicitly structured media and resource links are preserved.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Callable
import uuid

from background_native_view import unwrap_mcp_result


MEDIA_TYPES = frozenset({
    "image", "localImage", "audio", "localAudio", "video", "localVideo",
    "resource", "resource_link", "file", "attachment", "image_url",
    "inputImage", "inputAudio", "input_image", "input_audio",
})
REFERENCE_KEYS = ("savedPath", "path", "url", "uri", "sourceRef",
                  "image_url", "imageUrl", "audio_url", "audioUrl",
                  "video_url", "videoUrl")
VISIBLE_PHASES = (None, "commentary", "final", "final_answer")
INCOMPLETE_REASON = (
    "Не удалось получить полную историю результатов. Найденные файлы показаны; "
    "повторите обновление, когда источник задачи будет доступен."
)
OMITTED_OUTPUT_REASON = (
    "Источник полной истории не вернул часть структурированных результатов "
    "инструментов; список файлов может быть неполным."
)


def _media(value) -> list[dict]:
    return [deepcopy(part) for part in value if isinstance(part, dict)
            and part.get("type") in MEDIA_TYPES] if isinstance(value, list) else []


def _structured_media(value) -> list[dict]:
    """Keep typed artifact descriptors from structured data, never free text."""
    if isinstance(value, dict):
        if value.get("type") in MEDIA_TYPES or any(
                isinstance(value.get(key), str) and value.get(key) for key in REFERENCE_KEYS):
            return [deepcopy(value)]
        output = []
        for child in value.values():
            output.extend(_structured_media(child))
        return output
    if isinstance(value, list):
        output = []
        for child in value:
            output.extend(_structured_media(child))
        return output
    return []


def _unique_media(*groups) -> list[dict]:
    output, seen = [], set()
    for group in groups:
        for part in group:
            key = json.dumps(part, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                output.append(part)
    return output


def _attachments(value) -> list[dict]:
    return [deepcopy(part) for part in value if isinstance(part, dict)] if isinstance(value, list) else []


def _truncated(value) -> bool:
    if isinstance(value, dict):
        if any(value.get(key) is True for key in (
            "truncated", "isTruncated", "textTruncated", "outputTruncated",
            "contentTruncated", "hasMoreItems",
        )):
            return True
        return any(_truncated(child) for child in value.values())
    return isinstance(value, list) and any(_truncated(child) for child in value)


def _output_omitted(item: dict) -> bool:
    """Detect result-bearing items stripped by the native history adapter."""
    kind = item.get("type")
    if kind == "mcpToolCall" and item.get("status") == "completed":
        return "result" not in item or item.get("result") is None
    if kind == "dynamicToolCall" and item.get("status") == "completed":
        return "contentItems" not in item
    if kind == "imageGeneration" and item.get("status") == "completed":
        return not any(item.get(key) for key in ("savedPath", "path", "result", "url", "imageUrl"))
    if kind == "imageView":
        return not item.get("path")
    return False


def _message(item: dict) -> dict | None:
    kind = item.get("type")
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    text, content, attachments = "", [], []
    if kind == "agentMessage" and item.get("phase") in VISIBLE_PHASES:
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        content = _media(item.get("content"))
        attachments = _attachments(item.get("attachments"))
    elif kind in ("imageGeneration", "imageView"):
        # Keep only the explicit native image descriptor, not internal fields.
        content = [{key: deepcopy(item[key]) for key in (
            "type", "savedPath", "path", "result", "url", "imageUrl", "mimeType"
        ) if key in item}]
    elif kind == "functionCallOutput":
        content = _media(item.get("output"))
        if not content:
            return None
    elif kind == "mcpToolCall":
        result = item.get("result")
        if isinstance(result, dict):
            content = _unique_media(
                _media(result.get("content")),
                _structured_media(result.get("structuredContent")),
            )
            attachments = _attachments(result.get("attachments"))
        app_ui = item.get("mcpAppUi")
        app_uri = app_ui.get("resourceUri") if isinstance(app_ui, dict) else item.get("mcpAppResourceUri")
        if isinstance(app_uri, str) and app_uri:
            content = _unique_media(content, [{"type": "resource_link", "uri": app_uri,
                                               "name": "MCP app resource"}])
        if not content and not attachments:
            return None
    elif kind == "dynamicToolCall":
        content = _media(item.get("contentItems"))
        if not content:
            return None
    else:
        return None
    return {"id": identifier, "role": "assistant", "text": text,
            "content": content, "attachments": attachments}


def _read_args(thread_id: str, cursor: str | None, host_id: str | None) -> dict:
    args = {"threadId": thread_id, "turnLimit": 20, "includeOutputs": True,
            "maxOutputCharsPerItem": 20_000}
    if cursor:
        args["cursor"] = cursor
    if host_id:
        args["hostId"] = host_id
    return args


def load_native_artifacts(thread_id: str, *,
                          host_id: str | None = None,
                          read_page: Callable | None = None) -> dict:
    """Return all history pages, or explicit incompleteness without raw errors.

    The authenticated route must validate owner/thread scope before calling.
    Injected readers are used only by isolated tests; production uses the
    installed Desktop read-only tool and closes its child on every path.
    """
    uuid.UUID(thread_id)
    result = {"messages": [], "attachments": [], "complete": True}
    client = None
    seen_messages, seen_attachments, cursors = set(), set(), set()
    omitted_outputs = False
    cursor = None
    try:
        if read_page is None:
            from native_discovery import discover_environment
            from native_mcp_client import NativeMcpClient, SERVICE_THREAD_ID
            client = NativeMcpClient(
                caller_thread_id=SERVICE_THREAD_ID,
                environment=discover_environment(),
                request_timeout=60,
            )
            client.start()

            def read_page(identifier, page_cursor):
                return client.call("read_thread", _read_args(identifier, page_cursor, host_id))

        while True:
            data = unwrap_mcp_result(read_page(thread_id, cursor))
            if _truncated(data):
                result["complete"] = False
            for attachment in _attachments(data.get("attachments")):
                key = json.dumps(attachment, sort_keys=True, ensure_ascii=False)
                if key not in seen_attachments:
                    seen_attachments.add(key)
                    result["attachments"].append(attachment)
            turns = data.get("turns")
            if not isinstance(turns, list):
                raise ValueError("native_turns_missing")
            for turn in turns:
                if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
                    result["complete"] = False
                    continue
                for item in turn["items"]:
                    if isinstance(item, dict) and _output_omitted(item):
                        omitted_outputs = True
                        result["complete"] = False
                    message = _message(item) if isinstance(item, dict) else None
                    if message and message["id"] not in seen_messages:
                        seen_messages.add(message["id"])
                        result["messages"].append(message)
            page = data.get("page", {})
            next_cursor = page.get("nextCursor") if isinstance(page, dict) else None
            has_more = page.get("hasMore") if isinstance(page, dict) else None
            if (has_more is True and not next_cursor) or (has_more is False and next_cursor):
                result["complete"] = False
            if not next_cursor:
                break
            if not isinstance(next_cursor, str) or next_cursor in cursors:
                result["complete"] = False
                break
            cursors.add(next_cursor)
            cursor = next_cursor
    except Exception:
        result["complete"] = False
    finally:
        if client is not None:
            client.close()
    if not result["complete"]:
        result["reason"] = OMITTED_OUTPUT_REASON if omitted_outputs else INCOMPLETE_REASON
    return result

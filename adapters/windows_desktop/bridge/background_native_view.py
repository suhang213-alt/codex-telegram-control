"""Pure projections of native MCP results; never reads files or conversations.

Native read_thread v1 has newest-first turns, chronological items per turn.
Codex preserves agent phase; the installed ChatGPT adapter omits it. Native
items do not always carry timestamps. Missing IDs, dates and phases are never
invented. Source: installed Codex 26.1013.4894.0 execution/app-initial bundles.
"""
from __future__ import annotations
import bridge_config as config

from copy import deepcopy
import json
import re
from typing import Any, Iterable

SERVICE_THREADS = frozenset({config.service_thread_id()})
MESSAGE_TYPES = {"userMessage": "user", "agentMessage": "assistant"}
MEDIA_TYPES = frozenset({"image", "localImage", "audio", "localAudio"})
CHOICE_TOOLS = frozenset({"request_user_input", "request_option_picker",
                          "request_onboarding_input"})
AGENT_PHASES = frozenset({"commentary", "final", "final_answer"})


class ViewError(ValueError):
    pass


def unwrap_mcp_result(result: dict) -> dict:
    """Return one native JSON object, failing closed on errors/ambiguity."""
    if not isinstance(result, dict):
        raise ViewError("native_result_not_object")
    if result.get("isError") is True:
        raise ViewError("native_tool_error")
    if isinstance(result.get("structuredContent"), dict):
        return deepcopy(result["structuredContent"])
    if any(key in result for key in ("threads", "turns", "pinnedThreads", "projects")):
        return deepcopy(result)
    content = result.get("content", result.get("contentItems"))
    if not isinstance(content, list):
        raise ViewError("native_content_missing")
    parsed = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in ("text", "inputText"):
            continue
        try:
            value = json.loads(item.get("text", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            parsed.append(value)
    if len(parsed) != 1:
        raise ViewError("native_json_missing_or_ambiguous")
    return parsed[0]


def normalize_threads(result: dict, allowed_thread_ids: Iterable[str] | None = None) -> list[dict]:
    """Codex is visible; ChatGPT requires an explicit permitted-ID set.

    Keep pinned UI order followed by native recency order, deduplicated.
    Service threads are excluded in either case.
    """
    data = unwrap_mcp_result(result)
    allowed = set() if allowed_thread_ids is None else set(allowed_thread_ids)
    seen = set()
    output = []
    for section, pinned in (("pinnedThreads", True), ("threads", False)):
        for row in data.get(section, []):
            if not isinstance(row, dict):
                continue
            identifier = row.get("id")
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                continue
            if identifier in SERVICE_THREADS:
                continue
            if row.get("kind") not in ("codex", "chatgpt"):
                continue
            if row["kind"] == "chatgpt" and identifier not in allowed:
                continue
            seen.add(identifier)
            status = row.get("status")
            if isinstance(status, dict):
                status = status.get("type")
            normalized = {
                "id": identifier, "kind": row["kind"], "title": row.get("title"),
                "status": status, "updatedAt": row.get("updatedAt"),
                "hostId": row.get("hostId"), "projectId": row.get("projectId"),
                "isPinned": pinned, "pinned": pinned, "cwd": row.get("cwd"),
            }
            if pinned:
                normalized["pinnedIndex"] = row.get("pinnedIndex")
            output.append(normalized)
    return output


def next_cursor(result: dict) -> str | None:
    """Native read_thread pagination is page.nextCursor, never root.nextCursor."""
    page = unwrap_mcp_result(result).get("page")
    cursor = page.get("nextCursor") if isinstance(page, dict) else None
    return cursor if isinstance(cursor, str) and cursor else None


def _chronological_items(data: dict):
    turns = data.get("turns", [])
    if not isinstance(turns, list):
        raise ViewError("native_turns_invalid")
    # Native v1 explicitly specifies this order. Do not sort lexical UUIDs.
    order = data.get("page", {}).get("order", "newest_first")
    if order not in ("newest_first", "oldest_first"):
        raise ViewError("native_turn_order_unknown")
    iterable = reversed(turns) if order == "newest_first" else iter(turns)
    output = []
    for turn in iterable:
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items", []):
            if isinstance(item, dict):
                output.append((turn, item))
    return output


def _text(item: dict) -> str:
    if item.get("type") == "agentMessage":
        if isinstance(item.get("text"), str):
            return item["text"]
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(part["text"] for part in content
                     if isinstance(part, dict) and part.get("type") == "text"
                     and isinstance(part.get("text"), str))


def _attachments(item: dict) -> list[dict]:
    output = []
    for part in item.get("content", []):
        if isinstance(part, dict) and part.get("type") in MEDIA_TYPES:
            output.append(deepcopy(part))
    for attachment in item.get("attachments", []):
        if isinstance(attachment, dict):
            output.append(deepcopy(attachment))
    return output


def flatten_thread(result: dict, max_messages: int = 30) -> list[dict]:
    """Only user/assistant messages, oldest first; at most 30 recent entries."""
    if not isinstance(max_messages, int) or isinstance(max_messages, bool) or not 0 <= max_messages <= 30:
        raise ViewError("message_limit_invalid")
    output = []
    seen = set()
    for _, item in _chronological_items(unwrap_mcp_result(result)):
        role = MESSAGE_TYPES.get(item.get("type"))
        identifier = item.get("id")
        if role is None or not isinstance(identifier, str) or not identifier or identifier in seen:
            continue
        # Exclude any explicitly marked internal/non-user-facing assistant phase.
        if role == "assistant" and item.get("phase") not in (None, *AGENT_PHASES):
            continue
        seen.add(identifier)
        output.append({
            "id": identifier, "role": role, "text": _text(item),
            "createdAt": item.get("createdAt"),
            "attachments": _attachments(item),
        })
    return output[-max_messages:] if max_messages else []


def extract_native_attachments(result: dict) -> list[dict]:
    """Thread-level native attachments. No invented association with a message."""
    data = unwrap_mcp_result(result)
    return deepcopy([item for item in data.get("attachments", []) if isinstance(item, dict)])


def _explicit_text_choices(text: str) -> list[dict]:
    """Conservative exact question followed immediately by explicit options.

    This recognizes presentation, not intent. Callers must still check
    openDecision and must never use these as system-approval buttons.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        question = line.strip()
        if not question.endswith("?") or not re.search(
            r"\b(выберите|выбираем|выбираете|выбрать|какой вариант|какой из|какую из|"
            r"which option|which one|choose)\b", question, re.IGNORECASE
        ):
            continue
        options = []
        started = False
        for next_line in lines[index + 1:]:
            if not next_line.strip() and not started:
                continue
            match = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$", next_line)
            if not match:
                break
            started = True
            options.append({"label": match.group(1)})
        if 2 <= len(options) <= 10:
            return [{"question": question, "options": options}]
    return []


def _async_questions(item: dict) -> list[dict]:
    """Preserve native async-question titles and option strings verbatim."""
    if item.get("delivery") != "async" or not isinstance(item.get("questions"), list):
        return []
    questions = []
    for source in item["questions"]:
        if not isinstance(source, dict) or not isinstance(source.get("title"), str) or not source["title"]:
            return []
        options = source.get("options")
        if options is not None and (not isinstance(options, list) or
                                    any(not isinstance(value, str) or not value for value in options)):
            return []
        questions.append({"title": source["title"], "question": source["title"],
                          "options": [{"label": value, "description": ""}
                                      for value in (options or [])]})
    return questions


def _async_answer_ids(item: dict) -> set[str]:
    """Recognize answered IDs; ordinary user text never closes an async question."""
    if item.get("type") != "userMessage":
        return set()
    text = _text(item).strip()
    start, end = "<send_user_message_question_reply>", "</send_user_message_question_reply>"
    if not text.startswith(start) or not text.endswith(end):
        return set()
    try:
        replies = json.loads(text[len(start):-len(end)])
    except ValueError:
        return set()
    if isinstance(replies, dict):
        replies = [replies]
    if not isinstance(replies, list) or not replies:
        return set()
    ids = {reply["questionItemId"] for reply in replies
           if isinstance(reply, dict) and isinstance(reply.get("questionItemId"), str)
           and isinstance(reply.get("answer"), str)
           and reply["answer"].strip()}
    return ids


def hydrate_async_agent_question(result: dict, item_id: str, turn_id: str,
                                 source_item: dict) -> dict:
    """Attach rollout-only choices to the *same* native-verified agent item.

    read_thread may omit ``delivery`` and ``questions`` even when the local
    item_completed record contains them.  Keep the native turn chronology and
    subsequent user replies intact so an already answered question is closed
    by its exact questionItemId, not by an unrelated later message.
    """
    data = unwrap_mcp_result(result)
    if (not isinstance(item_id, str) or not item_id or
            not isinstance(turn_id, str) or not turn_id or
            not isinstance(source_item, dict) or
            source_item.get("type") != "AgentMessage" or
            source_item.get("id") != item_id or
            not _async_questions(source_item)):
        raise ViewError("rollout_async_question_invalid")
    hydrated = deepcopy(data)
    matches = []
    for turn in hydrated.get("turns", []):
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items", []):
            if isinstance(item, dict) and item.get("id") == item_id:
                matches.append((turn, item))
    if (len(matches) != 1 or matches[0][0].get("id") != turn_id or
            matches[0][1].get("type") != "agentMessage"):
        raise ViewError("native_async_item_mismatch")
    native_item = matches[0][1]
    source_phase = source_item.get("phase")
    native_phase = native_item.get("phase")
    if (source_phase is not None and native_phase is not None and
            source_phase != native_phase):
        raise ViewError("native_async_phase_mismatch")
    if native_item.get("delivery") not in (None, "async"):
        raise ViewError("native_async_delivery_mismatch")
    if ("questions" in native_item and native_item["questions"] and
            native_item["questions"] != source_item["questions"]):
        raise ViewError("native_async_questions_mismatch")
    native_item["delivery"] = "async"
    native_item["questions"] = deepcopy(source_item["questions"])
    return hydrated


def extract_agent_events(result: dict) -> list[dict]:
    """Exact agent item IDs and phases; retain chronology and response evidence."""
    data = unwrap_mcp_result(result)
    items = _chronological_items(data)
    message_positions = [index for index, (_, item) in enumerate(items)
                         if item.get("type") in MESSAGE_TYPES]
    last_message = max(message_positions, default=-1)
    output = []
    seen = set()
    for index, (turn, item) in enumerate(items):
        identifier = item.get("id")
        if (item.get("type") != "agentMessage" or not isinstance(identifier, str)
                or not identifier or identifier in seen):
            continue
        phase = item.get("phase")
        if phase is not None and phase not in AGENT_PHASES:
            continue
        seen.add(identifier)
        later_user = any(following.get("type") == "userMessage"
                         for _, following in items[index + 1:])
        text = _text(item)
        async_questions = _async_questions(item)
        questions = (async_questions if item.get("delivery") == "async" else
                     _explicit_text_choices(text))
        source = ("native_async" if async_questions else
                  "explicit_text" if questions else None)
        if source == "native_async":
            later_inputs = [_async_answer_ids(following) for _, following in items[index + 1:]
                            if following.get("type") == "userMessage"]
            answer_ids = set().union(*later_inputs)
            pending_indices = [question_index for question_index in range(len(questions))
                               if json.dumps(["request_user_input_async", identifier, question_index],
                                             ensure_ascii=False, separators=(",", ":")) not in answer_ids]
        else:
            pending_indices = list(range(len(questions))) if not later_user else []
        output.append({
            "itemId": identifier, "turnId": turn.get("id"),
            "phase": phase, "text": text, "createdAt": item.get("createdAt"),
            "questions": questions, "questionSource": source,
            "pendingIndices": pending_indices,
            "hasSubsequentUserMessage": later_user,
            "answered": bool(questions) and not pending_indices,
            "openDecision": bool(pending_indices) and
                            (source == "native_async" or index == last_message),
        })
    return output


def extract_choice_requests(result: dict) -> list[dict]:
    """Explicit structured user-choice requests, never system permission requests."""
    items = _chronological_items(unwrap_mcp_result(result))
    output = []
    for index, (turn, item) in enumerate(items):
        tool = item.get("tool", item.get("name"))
        if tool not in CHOICE_TOOLS or item.get("type") not in (
            "dynamicToolCall", "mcpToolCall", "functionCall"
        ):
            continue
        identifier = item.get("id")
        arguments = item.get("arguments")
        if not isinstance(identifier, str) or not identifier or not isinstance(arguments, dict):
            continue
        questions = arguments.get("questions")
        if not isinstance(questions, list) or not questions:
            continue
        valid = [deepcopy(question) for question in questions
                 if isinstance(question, dict) and isinstance(question.get("question"), str)
                 and (question.get("options") is None or
                      isinstance(question.get("options"), list))]
        if not valid:
            continue
        later_user = any(following.get("type") == "userMessage"
                         for _, following in items[index + 1:])
        pending = item.get("status") in ("inProgress", "pending", "waiting")
        output.append({
            "itemId": identifier, "turnId": turn.get("id"), "tool": tool,
            "questions": valid, "hasSubsequentUserMessage": later_user,
            "answered": later_user,
            "openDecision": pending and not later_user,
            "questionSource": "native_sync",
        })
    return output

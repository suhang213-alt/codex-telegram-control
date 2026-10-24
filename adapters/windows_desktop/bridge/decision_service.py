"""Owner decisions shared by the Telegram bot and the authenticated Mini App.

Question and answer bodies live only in the bridge's DPAPI-encrypted store.
Native queue and native Desktop dispatch receipts are not treated as completed
answers until the exact native item is observed.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlencode


_LOCK = threading.RLock()
_LOCK_TIMEOUT = 3.0
_ID = re.compile(r"[a-f0-9]{20}\Z")
_SOURCES = {"native_async", "explicit_text", "native_sync", "native_gateway"}
_MAX_ANSWER = 3500
CUSTOM_PROMPT_PREFIX = "✍️ Ответ на решение "
_SECRET_NOTICE = "Секретный запрос. Ответьте на него непосредственно в Codex Desktop."


@contextmanager
def _store_transaction(bridge):
    """Serialize the whole encrypted-store read/modify/write across processes.

    The lock file holds no decision data and is deliberately retained.  The OS
    releases its byte-range lock if a process dies, unlike a sentinel file.
    Network sends and native dispatch must happen outside this short section.
    """
    path = bridge.DECISION_PROMPTS.with_name(bridge.DECISION_PROMPTS.name + ".lock")
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            deadline = time.monotonic() + _LOCK_TIMEOUT
            while True:
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if (exc.errno not in (errno.EACCES, errno.EAGAIN) and
                            getattr(exc, "winerror", None) not in (33, 36)):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("decision_store_busy") from exc
                    time.sleep(0.025)
            yield
        finally:
            try:
                if acquired:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _telegram_chunks(value: str, limit: int = 3900) -> list[str]:
    """Split on Unicode characters, respecting Telegram's UTF-16 length."""
    chunks, current, units = [], [], 0
    for character in value:
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > limit:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(character)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks


def build_async_decision_reply(call_id: str, index: int, question: str, answer: str) -> str:
    """Use the same structured user-message envelope as Desktop's async UI."""
    if not isinstance(call_id, str) or not call_id or type(index) is not int or index < 0:
        raise ValueError("invalid_async_question_id")
    if not isinstance(question, str) or not question or not isinstance(answer, str) or not answer.strip():
        raise ValueError("invalid_async_answer")
    item_id = json.dumps(["request_user_input_async", call_id, index], ensure_ascii=False,
                         separators=(",", ":"))
    body = [{"questionItemId": item_id, "question": question, "answer": answer}]
    return ("<send_user_message_question_reply>\n" +
            json.dumps(body, ensure_ascii=False, separators=(",", ":")) +
            "\n</send_user_message_question_reply>\n")


def _options(values):
    if not isinstance(values, list) or len(values) > 30:
        raise ValueError("invalid_decision_options")
    result = []
    for value in values:
        if isinstance(value, str):
            value = {"label": value, "description": ""}
        if not isinstance(value, dict):
            raise ValueError("invalid_decision_option")
        label, description = value.get("label"), value.get("description", "")
        if not isinstance(label, str) or not label or len(label) > 2000 or not isinstance(description, str) or len(description) > 4000:
            raise ValueError("invalid_decision_option")
        result.append({"id": str(len(result)), "label": label, "description": description})
    return result


def _load(bridge):
    rows = bridge.read_json(bridge.DECISION_PROMPTS) if bridge.DECISION_PROMPTS.exists() else {}
    if not isinstance(rows, dict):
        raise ValueError("invalid_decision_store")
    for row in rows.values():
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("options"), list):
            row["options"] = _options(row["options"])
        row.setdefault("questionSource", "explicit_text")
        row.setdefault("questionIndex", 0)
    return rows


def _save(bridge, rows):
    bridge.atomic_json(bridge.DECISION_PROMPTS, rows)


def _native_answer_text(row):
    answer = row.get("answer")
    if not isinstance(answer, dict) or not isinstance(answer.get("text"), str):
        raise ValueError("decision_answer_missing")
    return (build_async_decision_reply(row["itemId"], row["questionIndex"],
                                       row["question"], answer["text"])
            if row.get("questionSource") == "native_async" else answer["text"])


@lru_cache(maxsize=64)
def _scan_native_answer(path_string, file_size, mtime_ns, thread_id, question_id,
                        question, answer, question_at, submitted_at, excluded_client,
                        observed_stage):
    """Observe one exact user item in a bounded, versioned chat rollout.

    File size and mtime are cache keys so repeated panel polls do not rescan
    a busy rollout; an append invalidates the result.  The cache stores no
    delivery assertion, only that the answer is visibly in this chat.
    """
    from miniapp_server import reverse_line_ranges
    from response_watch import MAX_RECORD, completed_item
    path = Path(path_string)
    expected = {"questionItemId": question_id, "question": question,
                "answer": answer}
    start_tag, end_tag = ("<send_user_message_question_reply>",
                          "</send_user_message_question_reply>")
    seen = 0
    covered = False
    with path.open("rb") as source:
        end = min(os.fstat(source.fileno()).st_size, file_size)
        for start, stop in reverse_line_ranges(source, end):
            if end - start > 16 * 1024 * 1024:
                return None  # An old answer is not worth blocking panel polls.
            if stop - start > MAX_RECORD:
                source.seek(start)
                prefix = source.read(min(stop - start, 2048))
                if b'"UserMessage"' in prefix or b'"item_completed"' in prefix:
                    return None
                continue
            source.seek(start)
            prefix = source.read(min(stop - start, 2048))
            if b'"item_completed"' not in prefix or b'"UserMessage"' not in prefix:
                continue
            source.seek(start)
            try:
                record = json.loads(source.read(stop - start))
                item = completed_item(record)
            except (ValueError, UnicodeDecodeError, TypeError):
                return None
            if item is None or item["kind"] != "UserMessage":
                continue
            payload = record.get("payload") or {}
            if not isinstance(payload, dict) or payload.get("thread_id", thread_id) != thread_id:
                return None
            if answer is None and payload.get("thread_id") != thread_id:
                return None
            stamp = item.get("createdAt")
            if stamp is None:
                return None
            if stamp < question_at:
                covered = True
                break
            text = item["text"].strip()
            if not text.startswith(start_tag) or not text.endswith(end_tag):
                continue
            try:
                answers = json.loads(text[len(start_tag):-len(end_tag)])
            except ValueError:
                continue
            if not isinstance(answers, list):
                continue
            if answer is None:
                # A real Desktop reply may answer several questions at once.
                # Match this exact source/index/text without inventing an
                # answer or claiming that Telegram delivered it.
                matches = [entry for entry in answers if isinstance(entry, dict)
                           and entry.get("questionItemId") == question_id
                           and entry.get("question") == question
                           and isinstance(entry.get("answer"), str) and entry["answer"].strip()]
                if len(matches) != 1:
                    continue
            elif len(answers) != 1 or answers[0] != expected:
                continue
            if not item.get("itemId") or not item.get("turnId") or not item.get("clientUserMessageId"):
                return None
            if excluded_client is not None and item["clientUserMessageId"] == excluded_client:
                return None  # This could be the bridge queue; its observer must bind it.
            if submitted_at is not None and stamp < submitted_at:
                return None  # A previous manual answer is not this operation's result.
            seen += 1
            if seen > 1:
                return None
        else:
            covered = True
    return observed_stage if covered and seen == 1 else None


def _native_answer_stage(bridge, row, delivery):
    """Expose a visible native answer without claiming bridge provenance."""
    reason = delivery.get("deliveryReason")
    external = row.get("answer") is None and row.get("operationId") is None
    if (row.get("questionSource") != "native_async" or
            type(row.get("at")) not in (int, float)):
        return None
    if not external and (reason not in {"native_queue_missing", "worker_result", "exact_gateway_user_ack"}
                         or not isinstance(row.get("answer"), dict)
                         or type(row.get("operationId")) is not int):
        return None
    excluded_client = None
    submitted_at = None
    if external:
        observed_stage = "answered_elsewhere"
    elif reason == "native_queue_missing":
        from fast_dispatch import dispatch_receipt
        receipt = dispatch_receipt(bridge.APP_DIR / "fast_dispatch.sqlite", row["operationId"])
        excluded_client = receipt.get("clientUserMessageId") if isinstance(receipt, dict) else None
        if (not isinstance(excluded_client, str) or not excluded_client or
                receipt.get("threadId") != row.get("threadId")):
            return None
        observed_stage = "answered_elsewhere"
    else:
        submitted_at = row.get("answerSubmittedAt")
        if type(submitted_at) not in (int, float) or submitted_at < row["at"]:
            return None
        observed_stage = "answer_seen_in_chat"
    from miniapp_server import local_codex_rollout
    path = local_codex_rollout(row["threadId"])
    if path is None:
        return None
    question_id = json.dumps(["request_user_input_async", row["itemId"],
                              row["questionIndex"]], ensure_ascii=False,
                             separators=(",", ":"))
    stat = path.stat()
    return _scan_native_answer(str(path), stat.st_size, stat.st_mtime_ns,
                               row["threadId"], question_id, row["question"],
                               None if external else row["answer"]["text"], row["at"], submitted_at,
                               excluded_client, observed_stage)


def _verified_pending_path(bridge, row):
    """Only publish the staged command belonging to this exact decision."""
    operation_id, inbox = row.get("operationId"), getattr(bridge, "INBOX", None)
    if type(operation_id) is not int or operation_id < 0 or not isinstance(inbox, Path):
        return None
    expected = _native_answer_text(row)
    for suffix in (".web_pending", ".fast_pending"):
        path = inbox / f"{operation_id}{suffix}"
        try:
            if not path.is_file():
                continue
            command = bridge.read_json(path)
        except FileNotFoundError:
            continue  # Another publisher may have renamed this same command.
        if (not isinstance(command, dict) or type(command.get("id")) is not int or
                command.get("id") != operation_id or
                command.get("kind") != "followup" or
                command.get("selected_thread_id") != row["threadId"] or
                command.get("text") != expected or command.get("attachments") or
                command.get("model") is not None or command.get("thinking") is not None):
            raise ValueError("decision_pending_identity_conflict")
        return path
    return None


def _publication_state(bridge, decision_id, operation_id, state):
    with _store_transaction(bridge):
        rows = _load(bridge)
        row = rows.get(decision_id)
        if (isinstance(row, dict) and row.get("operationId") == operation_id and
                row.get("answer") is not None and row.get("publicationState") != state):
            row["publicationState"] = state
            _save(bridge, rows)


def _resume_publication(bridge, decision_id, row):
    """Retry only the same durable reservation, never prepare another command."""
    if row.get("status") != "submitting" or row.get("publicationState") == "published":
        return
    operation_id = row.get("operationId")
    try:
        if _verified_pending_path(bridge, row) is None:
            return  # Already published, or another process is finishing it.
        bridge.finish_web(operation_id)
        # A concurrent fast publisher may have returned "busy" while retaining
        # its staged file. Leave it recoverable if that publisher later dies.
        state = "uncertain" if _verified_pending_path(bridge, row) else "published"
    except Exception:
        _publication_state(bridge, decision_id, operation_id, "uncertain")
    else:
        _publication_state(bridge, decision_id, operation_id, state)


def _validate_payload(payload, bridge):
    if not isinstance(payload, dict):
        raise ValueError("invalid_decision")
    thread, item = payload.get("threadId"), payload.get("itemId")
    title, question = payload.get("title"), payload.get("question")
    source = payload.get("questionSource", "explicit_text")
    index = payload.get("questionIndex", 0)
    if (not all(isinstance(x, str) and x for x in (thread, item, title, question)) or
            len(thread) > 100 or len(item) > 150 or len(title) > 200 or
            len(question) > 100_000 or source not in _SOURCES or
            type(index) is not int or not 0 <= index < 100):
        raise ValueError("invalid_decision")
    bridge.require_thread_scope(thread)
    secret = payload.get("isSecret") is True
    options = [] if secret else _options(payload.get("options", []))
    if secret:
        question = _SECRET_NOTICE
    expected = json.dumps(["request_user_input_async", item, index], ensure_ascii=False,
                          separators=(",", ":"))
    if source == "native_async" and payload.get("questionItemId", expected) != expected:
        raise ValueError("invalid_async_question_id")
    decision_id = hashlib.sha256(f"{thread}:{item}:{index}".encode()).hexdigest()[:20]
    gateway = payload.get("gatewayRequest") if source == "native_gateway" else None
    if source == "native_gateway":
        from native_gateway_protocol import _seal
        if (not isinstance(gateway, dict) or gateway.get("key") != _seal(gateway) or
                gateway.get("threadId") != thread or item != "gateway-" + gateway["key"]):
            raise ValueError("invalid_gateway_request")
    status = "desktop_required" if source == "native_sync" or secret or gateway and gateway.get("desktopRequired") else "notifying"
    if source == "native_async":
        minimum = build_async_decision_reply(item, index, question, "x")
        if _utf16_length(minimum) > 12000:
            status = "desktop_required"
    return decision_id, {"threadId": thread, "itemId": item, "title": title,
                         "question": question, "options": options, "questionSource": source,
                         "questionIndex": index,
                         "questionItemId": expected if source == "native_async" else None,
                         "status": status, "secret": secret,
                         "notificationState": "ready", "nextChunk": 0,
                         "at": time.time(),
                         **({"gatewayRequest": gateway, "gatewayContext": payload.get("gatewayContext"),
                             "customAnswerAllowed": payload.get("customAnswerAllowed") is True} if gateway else {})}


def _message_chunks(record):
    """Preserve all text in order, within Telegram's UTF-16 message limit."""
    heading = "❗ Нужен ответ · " + record["title"] + "\n\n"
    question = record["question"]
    tail = "\n\n" + "\n".join(
        f"{int(option['id']) + 1}. {option['label']}" +
        (" — " + option["description"] if option["description"] else "")
        for option in record["options"])
    if record["status"] == "desktop_required":
        tail += "\n\nЭтот вопрос требует ответа в Codex Desktop."
    return _telegram_chunks(heading + question + tail)


def _buttons(bridge, record, decision_id):
    if record["status"] == "desktop_required":
        return []
    rows = [[{"text": f"{int(item['id']) + 1}. {item['label'][:48]}",
              "callback_data": f"answer:{decision_id}:{item['id']}"}]
            for item in record["options"]]
    if record.get("customAnswerAllowed", True):
        rows.append([{"text": "✍️ Свой вариант", "callback_data": "custom:" + decision_id}])
    try:
        from desktop_transport import miniapp_status
        path = bridge.APP_DIR / "miniapp_status.bin"
        status = miniapp_status(lambda: bridge.read_json(path) if path.is_file() else {})
        if status["healthy"]:
            url = status["url"].rstrip("/") + "/?" + urlencode({
                "thread": record["threadId"], "decision": decision_id})
            rows.append([{"text": "Открыть в панели", "web_app": {"url": url}}])
    except Exception:
        pass
    return rows


def register(bridge, payload):
    decision_id, record = _validate_payload(payload, bridge)
    with _store_transaction(bridge):
        rows = _load(bridge)
        if record["questionSource"] == "native_sync":
            gateway_match = next(((key, value) for key, value in rows.items()
                                  if value.get("questionSource") == "native_gateway" and
                                  value.get("threadId") == record["threadId"] and
                                  value.get("gatewayRequest", {}).get("itemId") == record["itemId"] and
                                  value.get("questionIndex") == record["questionIndex"]), None)
            if gateway_match:
                return {"id": gateway_match[0], "queued": False, "status": gateway_match[1].get("status")}
        legacy_id = hashlib.sha256(f"{record['threadId']}:{record['itemId']}".encode()).hexdigest()[:20]
        if record["questionIndex"] == 0 and legacy_id in rows and decision_id not in rows:
            old = rows[legacy_id]
            if (old.get("question") == record["question"] and
                    old.get("options") == record["options"]):
                return {"id": legacy_id, "queued": False, "status": old.get("status")}
        if decision_id in rows:
            old = rows[decision_id]
            if any(old.get(key) != record.get(key) for key in
                   ("threadId", "itemId", "questionIndex", "question", "options", "questionSource")):
                raise ValueError("decision_identity_conflict")
            if old.get("answer") is not None:
                if old.get("notificationState") == "ready":
                    old["notificationState"] = "complete"
                    _save(bridge, rows)
                return {"id": decision_id, "queued": False, "status": old.get("status")}
            if old.get("notificationState") != "ready":
                if (old.get("notificationState") == "sending" and
                        time.time() - float(old.get("notificationAt", 0)) > 120):
                    old["notificationState"] = "uncertain"
                    old["status"] = "notification_uncertain"
                    _save(bridge, rows)
                return {"id": decision_id, "queued": False, "status": old.get("status")}
            record = old
        else:
            rows[decision_id] = record
            _save(bridge, rows)
    try:
        chunks = _message_chunks(record)
        for number in range(record.get("nextChunk", 0), len(chunks)):
            with _store_transaction(bridge):
                rows = _load(bridge)
                row = rows[decision_id]
                if row.get("notificationState") != "ready" or row.get("nextChunk") != number:
                    return {"id": decision_id, "queued": False, "status": row.get("status")}
                row["notificationState"] = "sending"
                row["notificationAt"] = time.time()
                _save(bridge, rows)
            chunk = chunks[number]
            bridge.reply_payload({"text": chunk,
                                  "buttons": _buttons(bridge, record, decision_id) if number == len(chunks) - 1 else None,
                                  "scopeThreadId": record["threadId"]})
            with _store_transaction(bridge):
                rows = _load(bridge)
                rows[decision_id]["nextChunk"] = number + 1
                rows[decision_id]["notificationState"] = "ready"
                _save(bridge, rows)
    except Exception as exc:
        # This bridge exception is raised before any outbox/network side effect.
        # Keep only that chunk retryable; an ordinary transport failure might
        # have sent it and must remain uncertain to avoid a duplicate.
        if isinstance(exc, getattr(bridge, "OwnerNotPaired", ())):
            with _store_transaction(bridge):
                rows = _load(bridge)
                row = rows[decision_id]
                if row.get("notificationState") == "sending":
                    row["notificationState"] = "ready"
                    if row.get("answer") is None:
                        row["status"] = "notifying"
                    _save(bridge, rows)
            raise
        with _store_transaction(bridge):
            rows = _load(bridge)
            row = rows[decision_id]
            if row.get("answer") is None:
                row["status"] = "notification_uncertain"
            row["notificationState"] = "uncertain"
            _save(bridge, rows)
        raise
    with _store_transaction(bridge):
        rows = _load(bridge)
        if rows[decision_id].get("answer") is None:
            rows[decision_id]["status"] = "desktop_required" if record["status"] == "desktop_required" else "open"
        rows[decision_id]["notificationState"] = "complete"
        _save(bridge, rows)
    return {"id": decision_id, "queued": True, "status": rows[decision_id]["status"]}


def _snapshot(bridge, decision_id, row, delivery_snapshot=None):
    stage = row.get("status", "uncertain")
    if row.get("questionSource") == "native_gateway":
        from native_gateway_bridge import snapshot_status
        stage = snapshot_status(bridge, row)
    if row.get("publicationState") == "uncertain":
        stage = "uncertain"
    if row.get("questionSource") == "native_async" and row.get("answer") is None:
        try:
            stage = _native_answer_stage(bridge, row, {}) or stage
        except (OSError, ValueError):
            pass  # Missing local evidence never closes a live question.
    operation = row.get("operationId")
    if operation is not None and delivery_snapshot is not None:
        try:
            delivery = delivery_snapshot(operation)
            delivery_stage = delivery.get("deliveryStage", "uncertain")
            # A failed publication is not repaired by an absent/pending receipt.
            if row.get("publicationState") != "uncertain" or delivery_stage in {
                    "received", "completed"}:
                stage = delivery_stage
            if stage == "completed" and delivery.get("deliveryReason") != "exact_native_final":
                stage = "uncertain"
            if stage == "uncertain":
                stage = _native_answer_stage(bridge, row, delivery) or stage
        except Exception:
            stage = "uncertain"
    answer = row.get("answer")
    return {"id": decision_id, "threadId": row["threadId"], "title": row["title"],
            "question": row["question"], "options": row["options"], "status": stage,
            "questionSource": row.get("questionSource", "explicit_text"),
            "answer": answer, "operationId": str(operation) if operation is not None else None,
            "createdAt": row.get("at"), "customAnswerAllowed": row.get("customAnswerAllowed", True),
            "details": ({"request": {k: v for k, v in row["gatewayRequest"].items()
                                       if k in {"command", "cwd", "reason", "permissions", "grantRoot", "requestedSchema"}},
                         "changes": row.get("gatewayContext")} if row.get("gatewayRequest") else None)}


def list_for_thread(bridge, thread_id, delivery_snapshot=None):
    bridge.require_thread_scope(thread_id)
    with _store_transaction(bridge):
        rows = _load(bridge)
        selected = [(key, value) for key, value in rows.items()
                    if isinstance(value, dict) and value.get("threadId") == thread_id and
                    value.get("status") != "superseded" and _ID.fullmatch(key)]
    ordered = sorted(selected, key=lambda x: x[1].get("at", 0), reverse=True)
    open_statuses = {"open", "notifying", "notification_uncertain", "desktop_required", "submitting",
                     "waiting_answers", "gateway_submitting", "uncertain"}
    visible = [item for item in ordered if item[1].get("status") in open_statuses]
    visible.extend([item for item in ordered if item[1].get("status") not in open_statuses][:30])
    visible.sort(key=lambda x: x[1].get("at", 0), reverse=True)
    recoverable = [(key, row) for key, row in visible if row.get("status") == "submitting"
                   and row.get("publicationState") != "published"]
    if recoverable:
        for key, row in recoverable:
            _resume_publication(bridge, key, row)
        with _store_transaction(bridge):
            refreshed = _load(bridge)
            visible = [(key, refreshed.get(key, row)) for key, row in visible]
    return {"decisions": [_snapshot(bridge, key, row, delivery_snapshot) for key, row in visible]}


def get(bridge, decision_id, delivery_snapshot=None, *, _recover=True):
    if not isinstance(decision_id, str) or not _ID.fullmatch(decision_id):
        raise ValueError("invalid_decision_id")
    with _store_transaction(bridge):
        row = _load(bridge).get(decision_id)
    if not isinstance(row, dict):
        raise ValueError("decision_not_found")
    bridge.require_thread_scope(row["threadId"])
    if _recover and row.get("status") == "submitting" and row.get("publicationState") != "published":
        _resume_publication(bridge, decision_id, row)
        with _store_transaction(bridge):
            row = _load(bridge)[decision_id]
    return _snapshot(bridge, decision_id, row, delivery_snapshot)


def show(bridge, decision_id):
    if not isinstance(decision_id, str) or not _ID.fullmatch(decision_id):
        raise ValueError("invalid_decision_id")
    with _store_transaction(bridge):
        row = _load(bridge).get(decision_id)
    if not isinstance(row, dict) or row.get("status") not in {
            "open", "notification_uncertain", "desktop_required"}:
        raise ValueError("decision_not_open")
    bridge.require_thread_scope(row["threadId"])
    chunks = _message_chunks(row)
    for index, chunk in enumerate(chunks):
        bridge.send(chunk, _buttons(bridge, row, decision_id) if index == len(chunks) - 1 else None)


def prompt_custom(bridge, decision_id):
    if not isinstance(decision_id, str) or not _ID.fullmatch(decision_id):
        raise ValueError("invalid_decision_id")
    with _store_transaction(bridge):
        rows = _load(bridge)
        row = rows.get(decision_id)
        if not isinstance(row, dict) or row.get("status") not in {"open", "notifying", "notification_uncertain"}:
            raise ValueError("decision_not_open")
        bridge.require_thread_scope(row["threadId"])
        owner = bridge.state().get("owner_chat_id")
        if owner is None:
            raise ValueError("owner_not_paired")
        prior = row.get("customPromptMessageId")
        if type(prior) is int:
            return prior
        if row.get("customPromptStatus") in {"sending", "uncertain"}:
            raise ValueError("custom_reply_prompt_unconfirmed_open_panel_or_reply_to_prompt")
        row["customPromptStatus"] = "sending"
        _save(bridge, rows)
    # The decision ID in the prompt lets a Telegram reply reach its exact
    # source chat even if the network accepted a send before the ID was saved.
    try:
        result = bridge.api("sendMessage", {"chat_id": owner,
            "text": CUSTOM_PROMPT_PREFIX + decision_id +
                    "\nНапишите свой вариант ответа. Ответьте именно на это сообщение — он уйдёт в исходный чат.",
            "reply_markup": {"force_reply": True, "input_field_placeholder": "Ваш ответ на этот вопрос"}})
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if type(message_id) is not int:
            raise ValueError("custom_reply_prompt_unconfirmed")
    except Exception:
        with _store_transaction(bridge):
            rows = _load(bridge)
            rows[decision_id]["customPromptStatus"] = "uncertain"
            _save(bridge, rows)
        raise
    with _store_transaction(bridge):
        rows = _load(bridge)
        rows[decision_id]["customPromptMessageId"] = message_id
        rows[decision_id]["customPromptStatus"] = "sent"
        _save(bridge, rows)
    return message_id


def custom_reply_target(bridge, reply_to_message_id, prompt_text=None):
    if type(reply_to_message_id) is not int:
        return None
    with _store_transaction(bridge):
        rows = _load(bridge)
        if isinstance(prompt_text, str) and prompt_text.startswith(CUSTOM_PROMPT_PREFIX):
            embedded = prompt_text[len(CUSTOM_PROMPT_PREFIX):].split("\n", 1)[0]
            row = rows.get(embedded) if _ID.fullmatch(embedded) else None
            if isinstance(row, dict) and bridge.thread_in_scope(row.get("threadId")):
                return embedded
            return None
        matching = [(key, row) for key, row in rows.items()
                    if isinstance(row, dict) and row.get("customPromptMessageId") == reply_to_message_id
                    and _ID.fullmatch(key)]
    if len(matching) != 1:
        return None
    decision_id, row = matching[0]
    return decision_id if bridge.thread_in_scope(row.get("threadId")) else None


def answer(bridge, decision_id, *, choice_id=None, text=None, delivery_snapshot=None):
    if not isinstance(decision_id, str) or not _ID.fullmatch(decision_id):
        raise ValueError("invalid_decision_id")
    if (choice_id is None) == (text is None):
        raise ValueError("choose_option_or_enter_text")
    with _store_transaction(bridge):
        gateway_row = _load(bridge).get(decision_id)
    if isinstance(gateway_row, dict) and gateway_row.get("questionSource") == "native_gateway":
        from native_gateway_bridge import answer as gateway_answer
        return gateway_answer(bridge, decision_id, choice_id=choice_id, text=text)
    with _store_transaction(bridge):
        rows = _load(bridge)
        row = rows.get(decision_id)
        if not isinstance(row, dict):
            raise ValueError("decision_not_found")
        bridge.require_thread_scope(row["threadId"])
        if row.get("status") == "desktop_required":
            raise ValueError("answer_required_in_codex_desktop")
        if choice_id is not None:
            options = row.get("options", [])
            selected = next((item for item in options if item.get("id") == str(choice_id)), None)
            if selected is None:
                raise ValueError("invalid_choice")
            value = selected["label"]
            submitted = {"choiceId": str(choice_id), "text": value}
        else:
            if not isinstance(text, str) or not text.strip() or len(text) > _MAX_ANSWER:
                raise ValueError("answer_must_be_1_to_3500_characters")
            value = text
            submitted = {"text": value}
        prior = row.get("answer")
        if prior is not None:
            if prior != submitted:
                raise ValueError("decision_already_answered_differently")
            operation_id = row.get("operationId")
            if type(operation_id) is not int:
                raise ValueError("decision_receipt_missing")
        else:
            if row.get("status") not in {"open", "notifying", "notification_uncertain"}:
                raise ValueError("decision_not_open")
            try:
                external_stage = _native_answer_stage(bridge, row, {})
            except (OSError, ValueError):
                external_stage = None
            if external_stage == "answered_elsewhere":
                raise ValueError("decision_not_open")
            native_text = (build_async_decision_reply(row["itemId"], row["questionIndex"], row["question"], value)
                           if row.get("questionSource") == "native_async" else value)
            if len(native_text.encode("utf-16-le")) // 2 > 12000:
                raise ValueError("native_answer_exceeds_limit")
            # A decision answer must reach the currently running Desktop turn.
            # The ordinary CLI fast path only enqueues a future turn, which can
            # leave this question waiting until the current work finishes.
            operation_id = bridge.prepare_web("followup", native_text, row["threadId"],
                                              {"decision_answer": True}, [])
            row["answer"], row["operationId"], row["status"] = submitted, operation_id, "submitting"
            row["answerSubmittedAt"] = time.time()
            row["publicationState"] = "prepared"
            rows[decision_id] = row
            _save(bridge, rows)
    _resume_publication(bridge, decision_id, row)
    return {"decision": get(bridge, decision_id, delivery_snapshot, _recover=False)}

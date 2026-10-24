"""Deterministic owner-command dispatcher. It never creates service chat turns."""
from __future__ import annotations
import bridge_config as config

import hashlib
import json
from pathlib import Path
import re
import time
from datetime import datetime
from urllib.parse import quote

from native_mcp_client import NativeError, SERVICE_THREAD_ID
from native_gateway_client import NativeGatewayClient, GatewayError
from native_delegation import matched_delegated_input_indices
from native_wait_fallback import SnapshotError, snapshot_from_read_thread
from background_native_view import (unwrap_mcp_result, normalize_threads, flatten_thread,
                                    extract_agent_events, extract_choice_requests, next_cursor,
                                    hydrate_async_agent_question, ViewError)

SERVICE_IDS = {config.service_thread_id()}
MUTATIONS = {"followup", "new_task", "thread_rename", "thread_pin",
             "thread_unpin", "thread_archive", "thread_restore"}
WATCH_WAIT_TIMEOUT_MS = 0
LOCAL_QUESTION_SCAN_BYTES = 64 * 1024 * 1024
LOCAL_QUESTION_LINE_BYTES = 1024 * 1024


class Deferred(RuntimeError):
    """Read-only evidence is not available yet; leave the original pending."""


def local_async_question(thread_id: str, item_id: str, *, source_offset=None,
                         source_size=None) -> tuple[str, dict]:
    """Read an exact local async request after read_thread verified its item ID.

    The resolver checks Codex's local metadata and confines the path to its
    sessions directory. No rollout text is returned unless both the embedded
    thread ID and the completed item ID match the native-selected chat.
    """
    if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", item_id):
        raise Deferred("exact_async_source_unavailable")
    from miniapp_server import local_codex_rollout, reverse_line_ranges
    path = local_codex_rollout(thread_id)
    if path is None:
        raise Deferred("exact_async_source_unavailable")
    located = source_offset is not None or source_size is not None
    if located and (not isinstance(source_offset, int) or isinstance(source_offset, bool) or
                    source_offset < 0 or not isinstance(source_size, int) or
                    isinstance(source_size, bool) or not 0 < source_size <= LOCAL_QUESTION_LINE_BYTES):
        raise Deferred("exact_async_source_offset_invalid")

    def completed_question(line):
        if not line.endswith(b"\n") or item_id.encode("ascii") not in line:
            return None
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            return None
        payload = record.get("payload") if isinstance(record, dict) else None
        if (not isinstance(record, dict) or record.get("type") != "event_msg" or
                not isinstance(payload, dict) or
                payload.get("type") != "item_completed" or
                payload.get("thread_id") != thread_id):
            return None
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("id") != item_id:
            return None
        turn_id = payload.get("turn_id")
        if (not isinstance(turn_id, str) or not turn_id or
                item.get("type") != "AgentMessage" or item.get("delivery") != "async" or
                not isinstance(item.get("questions"), list) or not item["questions"]):
            raise Deferred("exact_async_source_invalid")
        return turn_id, item

    found = None
    try:
        with path.open("rb") as source:
            end = source.seek(0, 2)
            if located:
                if source_offset + source_size > end:
                    raise Deferred("exact_async_source_offset_invalid")
                if source_offset:
                    source.seek(source_offset - 1)
                    if source.read(1) != b"\n":
                        raise Deferred("exact_async_source_offset_invalid")
                source.seek(source_offset)
                found = completed_question(source.read(source_size))
                if found is None:
                    raise Deferred("exact_async_source_offset_mismatch")
                return found
            lower_bound = max(0, end - LOCAL_QUESTION_SCAN_BYTES)
            for start, stop in reverse_line_ranges(source, end):
                if start < lower_bound:
                    break
                size = stop - start
                if size > LOCAL_QUESTION_LINE_BYTES:
                    continue
                source.seek(start)
                candidate = completed_question(source.read(size))
                if candidate is not None:
                    if found is not None:
                        # A repeated item ID in one rollout is not safe evidence.
                        raise Deferred("exact_async_source_ambiguous")
                    found = candidate
    except OSError as exc:
        raise Deferred("exact_async_source_unavailable") from exc
    if found is None:
        raise Deferred("exact_async_source_unavailable")
    return found


def encoded(value):
    return quote(json.dumps(value, ensure_ascii=False, separators=(",", ":")), safe="")


def fingerprint(record):
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def timestamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 100_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def utf16_units(value):
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def final_reply_text(value):
    """Fit Telegram's UTF-16 limit without splitting a Unicode code point."""
    limit = 3900
    if utf16_units(value) <= limit:
        return value
    suffix = "\n\n… Полный ответ — по кнопке «Открыть переписку»."
    budget = limit - utf16_units(suffix)
    width = 0
    end = 0
    for char in value:
        next_width = 2 if ord(char) > 0xFFFF else 1
        if width + next_width > budget:
            break
        width += next_width
        end += 1
    prefix = value[:end].rstrip()
    boundary = max(prefix.rfind(" "), prefix.rfind("\n"))
    if boundary >= len(prefix) - 80 and boundary > 0:
        prefix = prefix[:boundary].rstrip()
    return prefix + suffix


def owner_prompt(record):
    text = record.get("text") or ""
    if (not isinstance(text, str) or
            len(text.encode("utf-16-le")) // 2 > (12000 if record.get("kind") == "followup" else 4000)):
        raise ValueError("invalid_owner_text")
    attachments = record.get("attachments") or []
    if not isinstance(attachments, list) or len(attachments) > 20:
        raise ValueError("invalid_owner_attachments")
    paths = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ValueError("invalid_owner_attachment")
        value = attachment.get("path")
        if not isinstance(value, str):
            raise ValueError("attachment_path_missing")
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("attachment_unavailable")
        paths.append(str(path))
    if paths:
        text += "\n\nПриложенные файлы:\n" + "\n".join(paths)
    if not text.strip():
        raise ValueError("empty_owner_prompt")
    return text


class Dispatcher:
    def __init__(self, bridge, native, ledger):
        self.bridge, self.native, self.ledger = bridge, native, ledger
        self.rows = {}
        self.watches = {}
        self.errors = {}  # fixed diagnostic codes, no message contents
        self._batch_rows = None
        self.parked = set()
        self.event_wait_only = False

    def new_event(self):
        """Only an actual input/desktop event permits revisiting parked idle work."""
        self.parked.clear()

    def immediate_gateway_inputs(self, records, attempted_ids):
        """Select verified local owner input before any native history read."""
        selected, rows = [], {}
        in_scope = getattr(self.bridge, "thread_in_scope", None)
        for record in records:
            target = record.get("selected_thread_id")
            if (str(record["id"]) in attempted_ids or record.get("kind") != "followup"
                    or target in SERVICE_IDS or not isinstance(target, str)
                    or record.get("attachments") or record.get("model") is not None
                    or record.get("thinking") is not None or not callable(in_scope)
                    or not in_scope(target)):
                continue
            row = self.rows.get(target)
            if row is None:
                try:
                    row = self.local_activity_target(target)
                except Deferred:
                    continue
            if row.get("kind") != "codex" or row.get("hostId") not in (None, "local"):
                continue
            if target not in rows:
                try:
                    NativeGatewayClient().discover(target)
                except GatewayError:
                    continue  # The full validation path retains the diagnostic.
                rows[target] = row
            selected.append(record)
        return selected, rows

    def correlation(self, row, prompt):
        data = self.detail(row)[0]
        turns = data.get("turns") or []
        latest = turns[0] if turns else {}
        messages = flatten_thread(data)
        result = {"promptDigest": hashlib.sha256(prompt.encode()).hexdigest()}
        if latest.get("id"):
            result["baselineTurnId"] = latest["id"]
        if messages:
            result["baselineItemId"] = messages[-1]["id"]
        return result

    def call(self, name, args, *, mutation=False):
        raw = self.native.call(name, args, owner_authorized=mutation)
        if raw.get("isError"):
            # A tool-level failure does not establish that a mutation had no effect.
            raise NativeError("native_tool_error", uncertain=mutation)
        return unwrap_mcp_result(raw)

    def refresh(self):
        result = self.call("list_threads", {"limit": 50})
        rows = normalize_threads(result, allowed_thread_ids=self.bridge.work_scope_ids())
        self.rows = {row["id"]: row for row in rows if row["id"] not in SERVICE_IDS and config.thread_allowed(row["id"])}
        self.bridge.cache_snapshot("cache_threads", encoded(list(self.rows.values())))
        for thread_id, row in self.rows.items():
            if (row.get("kind") == "codex" and
                    row.get("status") in {"inProgress", "active", "running", "working", "needsAttention"}
                    and thread_id not in self.watches and thread_id not in self.parked):
                self.watches[thread_id] = {"row": row, "observe": True, "afterCursor": None}
        return self.rows

    def target(self, target, *, archived=False):
        if not isinstance(target, str) or target in SERVICE_IDS or not config.thread_allowed(target):
            raise ValueError("service_or_invalid_target")
        if archived:
            cursor, collected = None, []
            for _ in range(10):
                args = {"source": "codex", "limit": 50}
                if cursor:
                    args["cursor"] = cursor
                result = self.call("list_archived_threads", args)
                rows = [{**x, "kind": "codex"} for x in result.get("threads", [])]
                collected.extend(rows)
                row = next((x for x in rows if x.get("id") == target), None)
                if row:
                    snapshot = [row, *[r for r in collected if r["id"] != target]][:100]
                    self.bridge.cache_snapshot("cache_archived", encoded(snapshot))
                    return row
                cursor = result.get("nextCursor")
                if not cursor:
                    break
        else:
            if self._batch_rows is None:
                self.refresh()
            row = self.rows.get(target)
            if row:
                return row
        raise Deferred("target_not_in_native_snapshot")

    def local_activity_target(self, target):
        """Resolve one rollout event beyond list_threads' hard 50-chat limit.

        Local metadata or a native-verified visible chat grants read-only
        correlation only. The exact item must still be confirmed by
        read_thread before any decision is published.
        """
        local_threads = getattr(self.bridge, "local_codex_threads", None)
        cached_threads = getattr(self.bridge, "cached_threads", None)
        in_scope = getattr(self.bridge, "thread_in_scope", None)
        known_chatgpt = getattr(self.bridge, "known_chatgpt_ids", None)
        if (not callable(local_threads) or not callable(in_scope) or
                not callable(known_chatgpt) or target in SERVICE_IDS):
            raise Deferred("activity_scope_unconfirmed")
        rows = local_threads()
        row = next((item for item in rows if isinstance(item, dict) and
                    item.get("id") == target and item.get("kind") == "codex" and
                    item.get("hostId") == "local"), None) if isinstance(rows, list) else None
        if row is None and callable(cached_threads):
            cached = cached_threads()
            row = next((item for item in cached if isinstance(item, dict) and
                        item.get("id") == target and item.get("kind") == "codex" and
                        item.get("hostId") in (None, "local") and
                        item.get("native_verification") == "codex_native_tools"),
                       None) if isinstance(cached, list) else None
        if row is None or target in known_chatgpt() or not in_scope(target):
            raise Deferred("activity_scope_unconfirmed")
        return row

    @staticmethod
    def host(row):
        return {"hostId": row["hostId"]} if row.get("hostId") else {}

    def detail(self, row, cursor=None, *, pages=1, include_outputs=False):
        collected, current = [], cursor
        for _ in range(pages):
            args = {"threadId": row["id"], "turnLimit": 3, "includeOutputs": include_outputs,
                    **self.host(row)}
            if include_outputs:
                args["maxOutputCharsPerItem"] = 20000
            if current:
                args["cursor"] = current
            result = self.call("read_thread", args)
            collected.append(result)
            current = next_cursor(result)
            if not current:
                break
        return collected

    def cache_detail(self, row, result, cursor=None):
        payload = {"id": row["id"], "messages": flatten_thread(result),
                   "nextCursor": next_cursor(result)}
        if cursor:
            payload["cursor"] = cursor
        self.bridge.cache_snapshot("cache_thread_page" if cursor else "cache_thread",
                                   encoded(payload))

    def process_batch(self, *, limit=8, attempted_ids=None, blocked_activity=None):
        """Process one bounded snapshot, then check the durable queue again.

        The caller retains both sets across snapshots from the same wake so an
        unsuccessful item is not retried in a tight loop or overtaken by a later
        activity item from its chat.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError("invalid_batch_limit")
        if attempted_ids is None:
            attempted_ids = set()
        if blocked_activity is None:
            blocked_activity = set()
        records = self.bridge.pending_records()
        active_watches = self.ledger.active_watches()
        if not records and not active_watches:
            return False
        immediate, immediate_rows = self.immediate_gateway_inputs(records, attempted_ids)
        if immediate:
            records = immediate
            self.rows.update(immediate_rows)
        elif records or not self.rows:
            self.refresh()
        for saved in active_watches:
            receipt = saved["receipt"]
            target = receipt.get("threadId")
            if (target in self.rows and
                    (target not in self.watches or self.watches[target].get("observe"))
                    and target not in self.parked):
                self.watches[target] = {
                    "row": self.rows[target], "record": None,
                    "operationId": saved["operation_id"], "receivedAt": saved["created_at"],
                    "afterCursor": saved.get("afterCursor"),
                    "receipt": receipt,
                }
        self._batch_rows = self.rows
        try:
            def priority(record):
                if record.get("decision_answer") is True:
                    return -1
                if record.get("kind") != "activity_event":
                    return 0  # Owner commands retain precedence over observations.
                return 1 if record.get("has_choices") is True else 2

            ordered = sorted(records, key=lambda r: (
                priority(r),
                str(r.get("source_at") or ""), int(r["id"])))
            attempted = 0
            for record in ordered:
                identifier = str(record["id"])
                target = record.get("selected_thread_id")
                if identifier in attempted_ids or (record.get("kind") == "activity_event" and
                                                    target in blocked_activity):
                    continue
                attempted_ids.add(identifier)
                attempted += 1
                try:
                    self.process(record)
                    self.errors.pop(identifier, None)
                except Exception as exc:
                    self.errors[identifier] = (
                        exc.code if isinstance(exc, NativeError) else
                        str(exc) if isinstance(exc, Deferred) else "background_operation_failed")
                    if record.get("kind") == "activity_event":
                        blocked_activity.add(target)
                    else:
                        state = self.ledger.get(identifier)
                        if state and state.state == "uncertain":
                            self.report_uncertain(record)
                if attempted >= limit:
                    break
        finally:
            self._batch_rows = None
        # Re-snapshot after this tranche: a new owner decision can outrank the
        # remaining history, even if it arrived after the initial snapshot.
        return any(
            str(record["id"]) not in attempted_ids and
            (record.get("kind") != "activity_event" or
             record.get("selected_thread_id") not in blocked_activity)
            for record in self.bridge.pending_records())

    def process(self, record):
        kind = record.get("kind")
        if kind == "activity_event":
            return self.activity(record)
        if kind == "watch_dispatched":
            if record.get("observer") == "directory_events":
                return
            row = self.target(record.get("selected_thread_id"))
            if row["id"] in self.parked:
                return
            self.watches[row["id"]] = {"row": row, "record": record,
                                      "receivedAt": record.get("received_at"),
                                      "afterCursor": None}
            return
        operation_id = str(record["id"])
        target = record.get("selected_thread_id") or "global"
        if kind == "new_task":
            target = "project_" + hashlib.sha256(target.encode()).hexdigest()
        operation = self.ledger.claim(operation_id, fingerprint(record), target).operation
        if operation.next_action in {"none", "manual_review", "in_flight"}:
            if operation.next_action == "manual_review":
                self.report_uncertain(record)
            return
        if operation.next_action == "ack":
            self.bridge.ack_pending(operation_id)
            self.ledger.confirm_ack(operation_id)
            return
        if operation.next_action == "execute":
            # Validate identity and attachments before persisting the effect boundary.
            try:
                plan = self.plan(record)
            except ValueError:
                plan = {"effect": lambda: {"status": "rejected"}}
            self.ledger.begin_execute(operation_id)
            try:
                receipt = plan["effect"]() if plan.get("effect") else {"status": "read_only"}
                operation = self.ledger.confirm_effect(operation_id, receipt=receipt)
            except Exception:
                self.ledger.mark_uncertain(operation_id)
                raise
        # A restarted confirmed mutation can only publish its receipt, never execute again.
        payload = self.reply(record, operation.receipt or {})
        self.ledger.begin_reply(operation_id)
        try:
            self.bridge.reply_payload(payload)
        except Exception:
            self.ledger.mark_uncertain(operation_id)
            raise
        self.ledger.confirm_reply(operation_id)
        self.bridge.ack_pending(operation_id)
        self.ledger.confirm_ack(operation_id)

    def report_uncertain(self, record):
        """One separate diagnostic; never replay the original mutation or reply."""
        identifier = "diagnostic_" + str(record["id"])
        digest = hashlib.sha256(identifier.encode()).hexdigest()
        state = self.ledger.claim(identifier, digest, "global").operation
        if state.next_action == "execute":
            self.ledger.begin_execute(identifier)
            state = self.ledger.confirm_effect(identifier, receipt={"status": "diagnostic"})
        if state.next_action == "reply":
            self.ledger.begin_reply(identifier)
            try:
                self.bridge.reply_payload({
                    "operationId": int(record["id"]), "ok": False, "scopeGeneric": True,
                    "dispatch_status": "uncertain",
                    "text": "Результат передачи не подтверждён. Автоматический повтор остановлен; "
                            "требуется проверка в Desktop.",
                })
            except Exception:
                self.ledger.mark_uncertain(identifier)
                return
            self.ledger.confirm_reply(identifier)
            self.ledger.confirm_ack(identifier)

    def plan(self, record):
        kind, target = record.get("kind"), record.get("selected_thread_id")
        if kind == "followup":
            row = self.target(target)
            if row.get("kind") == "chatgpt" and record.get("attachments"):
                raise ValueError("work_attachments_unsupported")
            prompt = owner_prompt(record)
            # The gateway writes a real owner message to Desktop's existing
            # engine, preserving source request replies and steering an active
            # turn immediately. Its text-only API cannot preserve overrides
            # or attachments, so those retain the full native tool route.
            gateway = None
            if (row.get("kind") == "codex" and row.get("hostId") in (None, "local")
                    and not record.get("attachments")
                    and record.get("model") is None and record.get("thinking") is None):
                try:
                    candidate = NativeGatewayClient()
                    candidate.discover(target)
                    gateway = candidate
                except GatewayError as exc:
                    if exc.code != "native_gateway_unavailable":
                        raise Deferred(exc.code) from None
            if gateway is not None:
                correlation = {"promptDigest": hashlib.sha256(prompt.encode()).hexdigest()}

                def owner_input():
                    try:
                        result = gateway.owner_input(thread_id=target, prompt=prompt,
                                                     operation_id=str(record["id"]))
                    except GatewayError as exc:
                        # No delegated fallback is safe after attempting this
                        # operation: its exact engine result may arrive later.
                        raise NativeError(exc.code, uncertain=exc.uncertain) from None
                    if result.get("status") == "rejected":
                        return {"threadId": target, "status": "rejected", **self.host(row)}
                    identifiers = {key: result.get(key) for key in ("turnId", "clientUserMessageId")}
                    if (result.get("status") != "accepted" or result.get("threadId") != target
                            or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value)
                                   for value in identifiers.values())):
                        raise NativeError("native_gateway_delivery_unconfirmed", uncertain=True)
                    receipt = {"threadId": target, "status": "sent", **self.host(row),
                               **correlation, **identifiers, "deliveryTransport": "native_gateway"}
                    self.watches[target] = {"row": row, "record": None, "afterCursor": None,
                                           "operationId": str(record["id"]),
                                           "receivedAt": record.get("received_at"), "receipt": receipt}
                    return receipt

                return {"effect": owner_input}
            correlation = self.correlation(row, prompt)
            for prior in self.ledger.active_watches():
                old = prior["receipt"]
                if (old.get("threadId") == target and
                        all(old.get(key) == correlation.get(key) for key in
                            ("promptDigest", "baselineTurnId", "baselineItemId"))):
                    raise Deferred("identical_command_awaiting_prior_result")
            args = {"threadId": target, "prompt": prompt}
            if row.get("kind") == "codex":
                args.update(self.host(row))
                for key in ("model", "thinking"):
                    if record.get(key):
                        args[key] = record[key]
            def effect():
                result = self.call("send_message_to_thread", args, mutation=True)
                self.watches[target] = {"row": row, "record": None, "afterCursor": None,
                                       "operationId": str(record["id"]),
                                       "receivedAt": record.get("received_at"),
                                       "receipt": correlation}
                return {"threadId": target, "status": "sent", **self.host(row), **correlation}
            return {"effect": effect}
        if kind == "new_task":
            projects = self.call("list_projects", {}).get("projects", [])
            self.bridge.cache_snapshot("cache_projects", encoded(projects))
            project = next((x for x in projects if x.get("projectId") == target), None)
            if project is None or not config.project_allowed(project):
                raise Deferred("project_not_in_native_snapshot")
            is_work = "chatgpt" in str(project.get("projectKind", "")).casefold()
            if is_work and record.get("attachments"):
                raise ValueError("work_attachments_unsupported")
            prompt = owner_prompt(record)
            args = {"prompt": prompt,
                    "target": ({"type": "chatgptWorkCloud", "projectId": target} if is_work else
                               {"type": "project", "projectId": target, "environment": {"type": "local"}})}
            if not is_work:
                for key in ("model", "thinking"):
                    if record.get(key):
                        args[key] = record[key]
            def create():
                result = self.call("create_thread", args, mutation=True)
                receipt = {k: result[k] for k in ("threadId", "clientThreadId", "hostId")
                           if isinstance(result.get(k), str)}
                if not receipt.get("threadId") and not receipt.get("clientThreadId"):
                    raise NativeError("creation_receipt_missing", uncertain=True)
                receipt["status"] = "created_work" if is_work else "created_codex"
                receipt["promptDigest"] = hashlib.sha256(prompt.encode()).hexdigest()
                return receipt
            return {"effect": create}
        if kind in MUTATIONS:
            row = self.target(target, archived=kind == "thread_restore")
            args = {"threadId": target, **self.host(row)}
            if row.get("kind") == "chatgpt":
                args["source"] = "chatgpt"
                args.pop("hostId", None)
            if kind == "thread_rename":
                title = record.get("text")
                if not isinstance(title, str) or not title.strip() or len(title) > 100:
                    raise ValueError("invalid_title")
                args.pop("hostId", None)
                args["title"] = title
                name = "set_thread_title"
            elif kind in {"thread_archive", "thread_restore"}:
                name = "set_thread_archived"
                args["archived"] = kind == "thread_archive"
            else:
                name = "move_thread_to_sidebar_section"
                args["sectionId"] = "pinned" if kind == "thread_pin" else None
            def update():
                self.call(name, args, mutation=True)
                return {"threadId": target, "status": "updated", **self.host(row)}
            return {"effect": update}
        # Only recognized user request kinds may produce responses.
        if kind not in {"active", "chats", "projects", "view", "select", "view_more",
                        "command", "callback", "help"}:
            raise Deferred("unsupported_command")
        return {}

    def reply(self, record, receipt):
        kind, target = record.get("kind"), record.get("selected_thread_id")
        payload = {"operationId": int(record["id"]), "ok": True}
        if receipt.get("status") == "rejected":
            return {**payload, "ok": False, "scopeGeneric": True,
                    "text": "Команда не выполнена: проверьте адресата, параметры и доступность вложений."}
        if kind in MUTATIONS:
            if kind == "new_task":
                target = receipt.get("threadId")
                if target and receipt.get("status") == "created_work":
                    self.bridge.register_created_work(target)
                if target:
                    try:
                        self.refresh()
                    except Exception:
                        # Creation is already committed; the generic receipt is safe.
                        target = None
                payload.update(text="Новый чат создан." if target else "Создание чата принято Desktop.")
            elif kind == "followup":
                payload["text"] = "Сообщение передано выбранному чату."
            else:
                payload["text"] = "Изменение чата выполнено."
            if target and self.bridge.thread_in_scope(target):
                payload["scopeThreadId"] = target
            else:
                payload["scopeGeneric"] = True
            return payload
        command = record.get("text") if kind in {"command", "callback"} else kind
        if command in {"view", "select", "view_more"} or str(command).startswith("select:"):
            target = str(command).split(":", 1)[1] if str(command).startswith("select:") else target
            row = self.target(target)
            cursor = record.get("text") if command == "view_more" else None
            result = self.detail(row, cursor)[0]
            self.cache_detail(row, result, cursor)
            payload.update(text="Переписка обновлена в панели.", scopeThreadId=target)
        elif command == "projects":
            rows = self.call("list_projects", {}).get("projects", [])
            self.bridge.cache_snapshot("cache_projects", encoded(rows))
            payload.update(text="Выберите проект.", buttons=[
                [{"text": str(x.get("label") or "Проект")[:60],
                  "callback_data": "newproject:" + x["projectId"]}] for x in rows[:30]
                if len(x["projectId"]) <= 53])
        elif command in {"active", "status", "chats"} or str(command).startswith("chats:page:"):
            rows = list(self.refresh().values())
            if command in {"active", "status"}:
                rows = [r for r in rows if r.get("status") in {"running", "inProgress", "needsAttention"}]
            page = int(str(command).split(":")[-1]) if str(command).startswith("chats:page:") else 0
            rows = rows[max(0, page) * 10:(max(0, page) + 1) * 10]
            payload.update(text=("Выберите чат." if rows else "Подходящих чатов в текущем списке нет."),
                           buttons=[[{"text": str(r.get("title") or "Чат")[:60],
                                      "callback_data": "select:" + r["id"]}] for r in rows])
        else:
            payload.update(text="/chats — чаты; /projects — проекты; /status — активные задачи; "
                                "/decisions — вопросы. Выберите чат и отправьте текст или вложение.",
                           scopeGeneric=True)
        return payload

    def activity(self, record):
        target = record.get("selected_thread_id")
        try:
            row = self.target(target)
        except Deferred as exc:
            if str(exc) != "target_not_in_native_snapshot":
                raise
            row = self.local_activity_target(target)
        if row.get("hostId") not in {None, "local"} or row.get("kind") != "codex":
            raise Deferred("activity_scope_unconfirmed")
        wanted = record.get("source_item_id")
        cursor, pages = None, []
        for _ in range(10):
            result = self.detail(row, cursor)[0]
            pages.append(result)
            # Inspect all newer pages together so only the exact async-question
            # answer ID closes that question. An unrelated later message is not
            # evidence that a native async question was answered.
            combined = {"page": {"order": "newest_first"}, "turns": [
                turn for page in pages for turn in page.get("turns", [])]}
            events = extract_agent_events(combined)
            found = next((e for e in events if e.get("itemId") == wanted), None)
            if found:
                if record.get("has_choices") is True and found.get("questionSource") != "native_async":
                    # read_thread confirms this exact item but can omit all
                    # async-choice metadata. Restore only those fields from the
                    # matching local completed event; keep native user replies.
                    source_turn_id, source_item = local_async_question(
                        target, wanted, source_offset=record.get("source_offset"),
                        source_size=record.get("source_size"))
                    try:
                        combined = hydrate_async_agent_question(
                            combined, wanted, source_turn_id, source_item)
                        found = next((event for event in extract_agent_events(combined)
                                      if event.get("itemId") == wanted), None)
                    except ViewError as exc:
                        raise Deferred("exact_async_source_mismatch") from exc
                    if not found or found.get("questionSource") != "native_async":
                        raise Deferred("exact_async_source_mismatch")
                self.cache_detail(row, result)
                if found.get("questions") and (found.get("answered") or
                                               not found.get("openDecision")):
                    self.bridge.ack_pending(record["id"])
                    return
                if found.get("questions"):
                    self.bridge.publish_native_question(row, found)
                else:
                    phase = found.get("phase")
                    # Intermediate messages already appear in the chat and its
                    # progress panel. Pushing each one to Telegram buries owner
                    # decisions and can flood the bot during backlog recovery.
                    if phase in {"final", "final_answer"}:
                        self.bridge.notice(encoded({"threadId": row["id"], "itemId": wanted,
                                                    "text": f"{row.get('title') or 'Чат Codex'} · Ответ готов."}))
                self.bridge.ack_pending(record["id"])
                return
            cursor = next_cursor(result)
            if not cursor:
                break
        raise Deferred("exact_source_item_unavailable")

    def publish_final(self, watch, exact):
        """Durably send a correlated final answer once; never replay an uncertain send."""
        origin = watch.get("operationId")
        if not origin:
            return False
        target = watch["row"]["id"]
        identity = "result_" + origin
        digest = hashlib.sha256(
            json.dumps([target, exact["turnId"], exact["itemId"], exact["text"]],
                       ensure_ascii=False).encode()).hexdigest()
        operation = self.ledger.claim(identity, digest, target).operation
        if operation.next_action == "execute":
            self.ledger.begin_execute(identity)
            operation = self.ledger.confirm_effect(identity, receipt={
                "status": "final_reply", "threadId": target,
                "turnId": exact["turnId"], "itemId": exact["itemId"],
            })
        if operation.next_action in {"manual_review", "in_flight"}:
            self.errors[identity] = "final_delivery_uncertain"
            return False
        if operation.next_action == "reply":
            text = final_reply_text(exact["text"])
            self.ledger.begin_reply(identity)
            try:
                self.bridge.reply_payload({
                    "operationId": int(origin), "scopeThreadId": target,
                    "ok": True, "text": text,
                    "buttons": [[{"text": "Открыть переписку", "callback_data": "view:" + target}]],
                })
            except Exception:
                self.ledger.mark_uncertain(identity)
                self.errors[identity] = "final_delivery_uncertain"
                return False
            operation = self.ledger.confirm_reply(identity)
        if operation.next_action == "ack":
            # Queue ACK is idempotent; the original command was already accepted.
            self.bridge.ack_pending(origin)
            self.ledger.confirm_ack(identity)
        return True

    def wait_once(self, *, attempted_ids=None):
        if not self.watches:
            return
        attempted_ids = attempted_ids or set()

        def owner_waiting():
            return any(str(record["id"]) not in attempted_ids and
                       record.get("kind") not in {"activity_event", "watch_dispatched"}
                       for record in self.bridge.pending_records())

        if owner_waiting():
            return
        # This Desktop version exposes wait_threads only for Codex-backed chats.
        for target in list(self.watches):
            if self.watches[target]["row"].get("kind") != "codex":
                self.parked.add(target)
                self.watches.pop(target)
        selected = list(self.watches)[:8]
        if not selected:
            return
        for thread_id in selected:
            self.watches[thread_id] = self.watches.pop(thread_id)
        targets = []
        for thread_id in selected:
            watch = self.watches[thread_id]
            target = {"threadId": thread_id, **self.host(watch["row"])}
            if watch.get("afterCursor"):
                target["afterCursor"] = watch["afterCursor"]
            targets.append(target)
        details = {}
        if not self.event_wait_only:
            try:
                # Windows events drive scheduling. Take only an immediate
                # snapshot here; native long waits hold up authenticated input.
                result = self.call("wait_threads", {"targets": targets,
                                                    "timeoutMs": WATCH_WAIT_TIMEOUT_MS})
            except NativeError as exc:
                if exc.code not in {"native_rpc_error", "native_tool_unavailable"}:
                    raise
                # The direct native connection can read a thread even when its
                # wait handler is unavailable. Further snapshots require an OS event.
                self.event_wait_only = True
        if self.event_wait_only:
            polls = []
            for thread_id in selected:
                details[thread_id] = self.detail(self.watches[thread_id]["row"])[0]
                try:
                    entry = snapshot_from_read_thread(
                        details[thread_id], expected_thread_id=thread_id,
                        expected_host_id=self.watches[thread_id]["row"].get("hostId"))
                    polls.append(entry)
                except SnapshotError as exc:
                    self.errors[thread_id] = str(exc)
                    self.parked.add(thread_id)
                    self.watches.pop(thread_id, None)
            result = {"polls": polls}
        # A terminal snapshot of a previous turn must never finish a new request.
        for entry in result.get("polls", []):
            if owner_waiting():
                return
            thread_id = entry.get("thread", {}).get("id")
            if thread_id not in self.watches:
                continue
            watch = self.watches[thread_id]
            if entry.get("cursor"):
                watch["afterCursor"] = entry["cursor"]
                if watch.get("operationId"):
                    self.ledger.save_watch_cursor(watch["operationId"], entry["cursor"])
            turn = entry.get("latestTurn") or {}
            detail = details.get(thread_id) or self.detail(watch["row"])[0]
            receipt = watch.get("receipt") or {}
            digest = receipt.get("promptDigest")
            if not digest and watch.get("record"):
                digest = watch["record"].get("expected_text_hash")
                started = timestamp(turn.get("startedAt"))
                received = timestamp(watch["record"].get("received_at"))
                if started is None or received is None or started < received:
                    self.parked.add(thread_id)
                    self.watches.pop(thread_id, None)
                    continue
            current_turn = next((t for t in detail.get("turns", []) if t.get("id") == turn.get("id")), None)
            # Correlation needs the full turn; the panel cache has a separate 30-message limit.
            raw_items = current_turn.get("items", []) if current_turn else []
            baseline_index = next((i for i, m in enumerate(raw_items)
                                   if m.get("id") == receipt.get("baselineItemId")), None)
            same_turn = bool(receipt.get("baselineTurnId") == turn.get("id"))
            matches = [i for i, item in enumerate(raw_items)
                       for m in flatten_thread({"turns": [{"items": [item]}]})
                       if m["role"] == "user" and hashlib.sha256(m["text"].encode()).hexdigest() == digest and
                       (not same_turn or baseline_index is not None and i > baseline_index)]
            if not matches and digest and watch.get("operationId"):
                if any(x.get("type") == "functionCallOutput" and
                       x.get("name") == "send_message_to_thread" and
                       x.get("namespace") == "codex_app" for x in raw_items):
                    detail_with_outputs = self.detail(watch["row"], include_outputs=True)[0]
                    complete_turn = next((t for t in detail_with_outputs.get("turns", [])
                                          if t.get("id") == turn.get("id")), None)
                    if complete_turn:
                        raw_items = complete_turn.get("items", [])
                        baseline_index = next((i for i, x in enumerate(raw_items)
                                               if x.get("id") == receipt.get("baselineItemId")), None)
                        matches = [i for i in matched_delegated_input_indices(
                            raw_items, source_thread_id=SERVICE_THREAD_ID, prompt_digest=digest)
                            if not same_turn or baseline_index is not None and i > baseline_index]
            if not matches and not watch.get("observe"):
                self.parked.add(thread_id)
                self.watches.pop(thread_id, None)
                continue
            self.cache_detail(watch["row"], detail)
            status = entry.get("thread", {}).get("status", {}).get("type")
            terminal = status in {"idle", "completed", "error", "needsAttention"}
            if terminal:
                message_id = entry.get("latestAssistantMessageId")
                exact = next((e for e in extract_agent_events(detail)
                              if e["itemId"] == message_id and e.get("turnId") == turn.get("id")), None)
                agent_index = next((i for i, m in enumerate(raw_items)
                                    if m.get("id") == message_id and m.get("type") == "agentMessage"), -1)
                if exact and matches and agent_index <= matches[-1]:
                    exact = None
                questions = [q for q in extract_choice_requests(detail)
                             if q.get("openDecision") and q.get("turnId") == turn.get("id")]
                if questions:
                    self.bridge.publish_native_question(watch["row"], questions[-1])
                elif exact and exact.get("openDecision"):
                    self.bridge.publish_native_question(watch["row"], exact)
                else:
                    if (watch.get("operationId") and status in {"idle", "completed"}
                            and not turn.get("error")
                            and (not exact or exact.get("phase") not in {"final", "final_answer"}
                                 or not exact.get("text"))):
                        # Status can arrive before the final message append.
                        self.parked.add(thread_id)
                        self.watches.pop(thread_id, None)
                        continue
                    item_id = message_id if exact else None
                    if not item_id and status in {"needsAttention", "error"}:
                        marker = entry.get("latestToolMarkerId")
                        marker_index = next((i for i, x in enumerate(raw_items)
                                             if x.get("id") == marker), -1)
                        user_raw_index = matches[-1] if matches else -1
                        if marker and marker_index > user_raw_index:
                            item_id = marker
                    if not item_id:
                        # No invented ID: retain until a source item becomes available.
                        self.parked.add(thread_id)
                        self.watches.pop(thread_id, None)
                        continue
                    action = ("Требуется действие в Desktop." if status == "needsAttention" else
                              "Задача завершилась с ошибкой." if turn.get("error") or status == "error" else
                              "Ответ готов.")
                    if (watch.get("operationId") and exact and matches and
                            exact.get("phase") in {"final", "final_answer"} and exact.get("text")):
                        if not self.publish_final(watch, exact):
                            self.parked.add(thread_id)
                            self.watches.pop(thread_id, None)
                            continue
                    else:
                        self.bridge.notice(encoded({"threadId": thread_id, "itemId": item_id,
                                                    "text": f"{watch['row'].get('title') or 'Чат'} · {action}"}))
                if watch.get("record"):
                    self.bridge.ack_pending(watch["record"]["id"])
                if watch.get("operationId"):
                    self.ledger.finish_watch(watch["operationId"])
                self.parked.add(thread_id)
                self.watches.pop(thread_id, None)

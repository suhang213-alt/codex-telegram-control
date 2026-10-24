"""Existing authenticated Telegram decisions backed by real engine requests."""
from __future__ import annotations

import json
import time

from native_gateway_protocol import INPUT, ELICITATION, build_response


def _title(bridge, thread_id):
    bridge.require_thread_scope(thread_id)
    for rows in (bridge.cached_threads(), bridge.local_codex_threads()):
        found = next((r for r in rows if r.get("id") == thread_id), None)
        if found:
            return found.get("title") or "Чат Codex"
    return "Чат Codex"


def publish_event(bridge, kind, envelope):
    import decision_service as decisions
    request = envelope["request"]
    target, key = request["threadId"], request["key"]
    if kind == "resolved":
        with decisions._store_transaction(bridge):
            rows = decisions._load(bridge)
            for row in rows.values():
                if row.get("questionSource") == "native_gateway" and row.get("gatewayRequest", {}).get("key") == key:
                    row["status"] = "accepted_by_server" if envelope["state"] == "accepted" and row.get("answer") is not None else "closed"
            decisions._save(bridge, rows)
        return
    if kind != "request":
        return
    title = _title(bridge, target)
    with decisions._store_transaction(bridge):
        rows = decisions._load(bridge)
        for row in rows.values():
            if (row.get("questionSource") == "native_sync" and row.get("threadId") == target
                    and row.get("itemId") == request.get("itemId")):
                row["status"] = "superseded"
        decisions._save(bridge, rows)
    questions = request.get("questions") if not request.get("desktopRequired") else None
    questions = questions or [{"question": request["question"], "options": request["options"]}]
    for index, question in enumerate(questions):
        options = question.get("options") or []
        payload = {"threadId": target, "itemId": "gateway-" + key, "title": title,
                   "questionSource": "native_gateway", "questionIndex": index,
                   "question": question["question"], "options": [
                       {"label": item["label"], "description": item.get("description", "")} for item in options],
                   "gatewayRequest": request, "gatewayContext": envelope.get("contextItem"),
                   "customAnswerAllowed": request["method"] in {INPUT, ELICITATION}}
        decisions.register(bridge, payload)


def snapshot_status(bridge, row):
    """Reconcile real ACK/lifecycle state, never repeat the response."""
    from native_gateway_client import NativeGatewayClient, GatewayError
    request = row["gatewayRequest"]
    try:
        result = NativeGatewayClient(timeout=1).list_requests(thread_id=row["threadId"])
        entry = next((e for e in result.get("requests", [])
                      if e.get("request", {}).get("key") == request["key"]), None)
        if entry is not None:
            state = entry.get("state")
            if state == "accepted" and row.get("answer") is not None:
                return "accepted_by_server"
            if state == "accepted":
                return "closed"
            if state in {"closed", "desktop_submitted"}:
                return "closed"
            if state == "pending" and row.get("status") == "gateway_submitting":
                return "uncertain"
        return row.get("status", "uncertain")
    except GatewayError:
        if row.get("status") in {"accepted_by_server", "closed"}:
            return row["status"]
        return "uncertain" if row.get("answer") is not None else "desktop_required"


def answer(bridge, decision_id, *, choice_id=None, text=None):
    import decision_service as decisions
    from native_gateway_client import NativeGatewayClient, GatewayError
    with decisions._store_transaction(bridge):
        candidate = decisions._load(bridge).get(decision_id)
    if not isinstance(candidate, dict) or candidate.get("questionSource") != "native_gateway":
        raise ValueError("gateway_decision_not_found")
    bridge.require_thread_scope(candidate["threadId"])
    if candidate.get("answer") is None or candidate.get("status") == "waiting_answers":
        try:
            live = NativeGatewayClient(timeout=2).list_requests(thread_id=candidate["threadId"])
        except GatewayError:
            raise ValueError("native_gateway_unavailable") from None
        entry = next((e for e in live.get("requests", [])
                      if e.get("request", {}).get("key") == candidate["gatewayRequest"]["key"]), None)
        if entry is None or entry.get("state") != "pending":
            with decisions._store_transaction(bridge):
                rows = decisions._load(bridge)
                rows[decision_id]["status"] = "closed"
                decisions._save(bridge, rows)
            return {"decision": decisions.get(bridge, decision_id, _recover=False)}
    with decisions._store_transaction(bridge):
        rows = decisions._load(bridge)
        row = rows.get(decision_id)
        if not isinstance(row, dict) or row.get("questionSource") != "native_gateway":
            raise ValueError("gateway_decision_not_found")
        bridge.require_thread_scope(row["threadId"])
        request = row["gatewayRequest"]
        if request.get("desktopRequired"):
            raise ValueError("protected_confirmation_requires_desktop")
        if choice_id is not None:
            selected = next((o for o in row["options"] if o["id"] == str(choice_id)), None)
            if selected is None:
                raise ValueError("invalid_choice")
            submitted = {"choiceId": str(choice_id), "text": selected["label"]}
        else:
            if not isinstance(text, str) or not text.strip() or len(text) > 3500:
                raise ValueError("answer_must_be_1_to_3500_characters")
            if not row.get("customAnswerAllowed"):
                raise ValueError("permission_requires_explicit_button")
            submitted = {"text": text}
        if row.get("answer") is not None:
            if row["answer"] != submitted:
                raise ValueError("decision_already_answered_differently")
            if row.get("status") != "waiting_answers":
                return {"decision": decisions._snapshot(bridge, decision_id, row)}
        elif row.get("status") not in {"open", "notifying", "notification_uncertain"}:
            raise ValueError("decision_not_open")
        group = [(key, value) for key, value in rows.items()
                 if value.get("questionSource") == "native_gateway" and value.get("gatewayRequest", {}).get("key") == request["key"]]
        if request["method"] == INPUT:
            answers = {request["questions"][value["questionIndex"]]["id"]: value["answer"]["text"]
                       for _, value in group if value.get("answer") is not None}
            answers[request["questions"][row["questionIndex"]]["id"]] = submitted["text"]
            if len(answers) < len(request["questions"]):
                row.update(answer=submitted, status="waiting_answers")
                decisions._save(bridge, rows)
                return {"decision": decisions._snapshot(bridge, decision_id, row)}
            response = build_response(request, answers=answers)
        else:
            response = build_response(request, choice=int(choice_id) if choice_id is not None else 0,
                                      text=text)
        operation_id = "decision-" + request["key"]
        for _, value in group:
            value.update(status="gateway_submitting", gatewayOperationId=operation_id)
        row.update(answer=submitted, answerSubmittedAt=time.time())
        decisions._save(bridge, rows)
    try:
        result = NativeGatewayClient(timeout=6).answer(thread_id=row["threadId"], request_key=request["key"],
                                                     response=response, operation_id=operation_id)
        status = "accepted_by_server" if result.get("status") == "accepted" else "uncertain"
    except GatewayError as exc:
        status = "closed" if not exc.uncertain and exc.code == "gateway_request_not_open" else "uncertain"
    with decisions._store_transaction(bridge):
        rows = decisions._load(bridge)
        for value in rows.values():
            if value.get("gatewayOperationId") == operation_id:
                value["status"] = status
        decisions._save(bridge, rows)
    return {"decision": decisions.get(bridge, decision_id, _recover=False)}

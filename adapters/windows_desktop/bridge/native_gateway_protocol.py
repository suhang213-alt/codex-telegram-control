"""Pure owner-request projection/validation for the installed app-server protocol.

No I/O, approvals, transport writes or retries happen here. A transport must bind
``key`` to the same live connection epoch and atomically select one responder.
The experimental protocol must pass the compatibility checks before use.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re

COMMAND = "item/commandExecution/requestApproval"
FILE = "item/fileChange/requestApproval"
PERMISSIONS = "item/permissions/requestApproval"
INPUT = "item/tool/requestUserInput"
ELICITATION = "mcpServer/elicitation/request"
LEGACY_COMMAND = "execCommandApproval"
LEGACY_FILE = "applyPatchApproval"
OWNER_METHODS = frozenset({COMMAND, FILE, PERMISSIONS, INPUT, ELICITATION,
                           LEGACY_COMMAND, LEGACY_FILE})
_BASIC = ("accept", "acceptForSession", "decline", "cancel")
_LABELS = {"accept": "Разрешить один раз", "acceptForSession": "Разрешить на сеанс",
           "decline": "Отклонить", "cancel": "Отменить действие",
           "approved": "Разрешить один раз", "approved_for_session": "Разрешить на сеанс",
           "abort": "Остановить действие"}
_SECRET = re.compile(r"password|passwd|secret|credential|api.?key|access.?token|refresh.?token|challenge", re.I)


class ProtocolError(ValueError):
    """Fixed diagnostic codes, never include question/answer contents."""


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ProtocolError("invalid_json_value") from None


def typed_key(request_id):
    if type(request_id) is int and -(2**63) <= request_id < 2**63:
        return "int:" + str(request_id)
    if type(request_id) is str and request_id:
        return "str:" + request_id
    raise ProtocolError("invalid_request_id")


def _same(left, right):
    return _json(left) == _json(right)


def _seal(value):
    return hashlib.sha256(_json({k: v for k, v in value.items() if k != "key"}).encode()).hexdigest()


def _check(normalized):
    if not isinstance(normalized, dict) or normalized.get("key") != _seal(normalized):
        raise ProtocolError("stale_or_modified_request")
    if normalized.get("method") not in OWNER_METHODS:
        raise ProtocolError("unsupported_request")
    typed_key(normalized.get("requestId"))
    if normalized.get("desktopRequired"):
        raise ProtocolError("desktop_required")


def _option(value, label=None):
    return {"label": label or _LABELS.get(value if isinstance(value, str) else "", "Разрешить по предложенному правилу"),
            "value": deepcopy(value)}


def _command_decision(value):
    if isinstance(value, str):
        return value in _BASIC
    if not isinstance(value, dict) or len(value) != 1:
        return False
    if set(value) == {"acceptWithExecpolicyAmendment"}:
        body = value["acceptWithExecpolicyAmendment"]
        return (isinstance(body, dict) and set(body) == {"execpolicy_amendment"}
                and isinstance(body["execpolicy_amendment"], list)
                and all(isinstance(x, str) for x in body["execpolicy_amendment"]))
    if set(value) == {"applyNetworkPolicyAmendment"}:
        body = value["applyNetworkPolicyAmendment"]
        rule = body.get("network_policy_amendment") if isinstance(body, dict) else None
        return (isinstance(body, dict) and set(body) == {"network_policy_amendment"} and isinstance(rule, dict)
                and set(rule) == {"action", "host"} and rule["action"] in ("allow", "deny")
                and isinstance(rule["host"], str))
    return False


def _permissions(value):
    if not isinstance(value, dict) or set(value) - {"fileSystem", "network"}:
        return False
    net = value.get("network")
    if net is not None and (not isinstance(net, dict) or set(net) - {"enabled"}
                            or net.get("enabled") is not None and type(net["enabled"]) is not bool):
        return False
    fs = value.get("fileSystem")
    if fs is not None:
        if not isinstance(fs, dict) or set(fs) - {"read", "write", "entries", "globScanMaxDepth"}:
            return False
        for key in ("read", "write"):
            if fs.get(key) is not None and (not isinstance(fs[key], list) or any(not isinstance(x, str) for x in fs[key])):
                return False
        if fs.get("entries") is not None:
            if not isinstance(fs["entries"], list):
                return False
            for entry in fs["entries"]:
                if not isinstance(entry, dict) or set(entry) != {"access", "path"} or entry["access"] not in ("read", "write", "deny") or not _filesystem_path(entry["path"]):
                    return False
        depth = fs.get("globScanMaxDepth")
        if depth is not None and (type(depth) is not int or depth < 1):
            return False
    return True


def _filesystem_path(value):
    if not isinstance(value, dict):
        return False
    kind = value.get("type")
    if kind == "path":
        return set(value) == {"type", "path"} and isinstance(value["path"], str)
    if kind == "glob_pattern":
        return set(value) == {"type", "pattern"} and isinstance(value["pattern"], str)
    if kind != "special" or set(value) != {"type", "value"} or not isinstance(value["value"], dict):
        return False
    special = value["value"]
    kind = special.get("kind")
    if kind in ("root", "minimal", "tmpdir", "slash_tmp"):
        return set(special) == {"kind"}
    if kind == "project_roots":
        return not set(special) - {"kind", "subpath"} and (special.get("subpath") is None or isinstance(special["subpath"], str))
    if kind == "unknown":
        return not set(special) - {"kind", "path", "subpath"} and isinstance(special.get("path"), str) and (special.get("subpath") is None or isinstance(special["subpath"], str))
    return False


def _subset(granted, requested):
    if not _permissions(granted):
        return False
    for category, body in granted.items():
        if body is None or body == {}:
            continue
        source = requested.get(category)
        if not isinstance(source, dict):
            return False
        if category == "fileSystem":
            entries = body.get("entries") or []
            # Removing a requested deny while granting its parent would turn a
            # syntactic subset into broader access. Retain all source denials.
            has_positive_grant = bool(body.get("read") or body.get("write") or any(x["access"] != "deny" for x in entries))
            if has_positive_grant and any(x["access"] == "deny" and not any(_same(x, y) for y in entries) for x in (source.get("entries") or [])):
                return False
        for field, val in body.items():
            if val is None:
                continue
            if field not in source or source[field] is None:
                return False
            if isinstance(val, list):
                if not isinstance(source[field], list) or any(not any(_same(x, y) for y in source[field]) for x in val):
                    return False
            elif field == "enabled":
                if val is True and source[field] is not True:
                    return False
            elif field == "globScanMaxDepth":
                if type(source[field]) is not int or val > source[field]:
                    return False
            elif not _same(val, source[field]):
                return False
    return True


def _form_supported(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object" or set(schema) - {"type", "properties", "required", "$schema", "additionalProperties"}:
        return False
    props = schema.get("properties")
    required = schema.get("required") or []
    if not isinstance(props, dict) or not isinstance(required, list) or any(x not in props for x in required):
        return False
    if schema.get("additionalProperties", False) is not False:
        return False
    for name, field in props.items():
        if _SECRET.search(name) or not isinstance(field, dict):
            return False
        kind = field.get("type")
        common = {"type", "title", "description", "default"}
        allowed = {"string": {"enum", "enumNames", "minLength", "maxLength"},
                   "number": {"minimum", "maximum"}, "integer": {"minimum", "maximum"},
                   "boolean": set()}.get(kind)
        if allowed is None or set(field) - common - allowed:
            return False
        if "enum" in field and (not isinstance(field["enum"], list) or not field["enum"] or any(not isinstance(x, str) for x in field["enum"])):
            return False
        if "enumNames" in field and (not isinstance(field["enumNames"], list) or len(field["enumNames"]) != len(field.get("enum", [])) or any(not isinstance(x, str) for x in field["enumNames"])):
            return False
        for bound in ("minLength", "maxLength"):
            if field.get(bound) is not None and (type(field[bound]) is not int or field[bound] < 0):
                return False
        for bound in ("minimum", "maximum"):
            if field.get(bound) is not None and (type(field[bound]) not in (int, float) or not math.isfinite(field[bound])):
                return False
    return True


def _form_content(schema, value):
    if not isinstance(value, dict) or set(value) - set(schema["properties"]):
        return False
    if any(x not in value for x in (schema.get("required") or [])):
        return False
    for name, val in value.items():
        field = schema["properties"][name]
        kind = field["type"]
        if kind == "string":
            if not isinstance(val, str) or "enum" in field and val not in field["enum"]:
                return False
            if len(val) < (field.get("minLength") or 0) or field.get("maxLength") is not None and len(val) > field["maxLength"]:
                return False
        elif kind == "boolean":
            if type(val) is not bool:
                return False
        else:
            if type(val) not in (int, float) or not math.isfinite(val) or kind == "integer" and type(val) is not int:
                return False
            if field.get("minimum") is not None and val < field["minimum"] or field.get("maximum") is not None and val > field["maximum"]:
                return False
    return True


def normalize_request(method, params, request_id, epoch):
    if method not in OWNER_METHODS:
        return None
    typed_key(request_id)
    if not isinstance(epoch, str) or not epoch or not isinstance(params, dict):
        raise ProtocolError("invalid_request_identity")
    thread = params.get("threadId", params.get("conversationId"))
    if not isinstance(thread, str) or not thread:
        raise ProtocolError("missing_thread_id")
    if method not in (ELICITATION, LEGACY_COMMAND, LEGACY_FILE):
        if any(not isinstance(params.get(k), str) or not params[k] for k in ("turnId", "itemId")):
            raise ProtocolError("missing_turn_or_item_id")
    elif method in (LEGACY_COMMAND, LEGACY_FILE) and (not isinstance(params.get("callId"), str) or not params["callId"]):
        raise ProtocolError("missing_call_id")
    value = {"requestId": request_id, "epoch": epoch, "method": method,
             "threadId": thread, "turnId": params.get("turnId"),
             "itemId": params.get("itemId", params.get("callId")),
             "approvalId": params.get("approvalId"),
             "payloadDigest": hashlib.sha256(_json(params).encode()).hexdigest(),
             "question": "", "options": [], "desktopRequired": False}
    # These are source-owned display fields; no paraphrasing or raw metadata.
    for field in ("reason", "command", "cwd", "kind", "grantRoot", "startedAtMs",
                  "networkApprovalContext", "additionalPermissions", "fileChanges"):
        if field in params:
            value[field] = deepcopy(params[field])
    if method in (COMMAND, FILE):
        value["question"] = params.get("reason") or params.get("command") or "Требуется разрешение владельца"
        decisions = params.get("availableDecisions") if method == COMMAND else None
        decisions = list(_BASIC) if decisions is None else decisions
        if not isinstance(decisions, list) or not decisions or any(not _command_decision(x) for x in decisions):
            raise ProtocolError("invalid_available_decisions")
        value["options"] = [_option(x) for x in decisions]
    elif method in (LEGACY_COMMAND, LEGACY_FILE):
        value["question"] = params.get("reason") or "Требуется разрешение владельца"
        value["options"] = [_option(x) for x in ("approved", "approved_for_session", "abort")]
        value["options"].insert(2, _option({"denied": {"rejection": ""}}, "Отклонить"))
    elif method == PERMISSIONS:
        requested = params.get("permissions")
        if not _permissions(requested):
            raise ProtocolError("invalid_permission_request")
        value["permissions"] = deepcopy(requested)
        value["question"] = params.get("reason") or "Разрешить запрошенный доступ?"
        value["options"] = [_option({"permissions": requested, "scope": scope}, label)
                            for scope, label in (("turn", "Разрешить для текущего действия"), ("session", "Разрешить на сеанс"))]
        value["options"].append(_option({"permissions": {}, "scope": "turn"}, "Отклонить"))
    elif method == INPUT:
        questions = params.get("questions")
        if not isinstance(questions, list) or not questions or any(not isinstance(q, dict) or not all(isinstance(q.get(k), str) and q[k] for k in ("id", "header", "question")) for q in questions):
            raise ProtocolError("invalid_questions")
        if len({q["id"] for q in questions}) != len(questions):
            raise ProtocolError("duplicate_question_id")
        if any(q.get("isSecret") is True for q in questions):
            value.update(desktopRequired=True, question="Требуется ввод защищённых данных в Desktop", notice="secret_input")
        else:
            value["questions"] = deepcopy(questions)
            value["question"] = "\n\n".join(q["question"] for q in questions)
            for q in questions:
                options = q.get("options")
                if options is not None and (not isinstance(options, list) or any(not isinstance(x, dict) or not isinstance(x.get("label"), str) or not isinstance(x.get("description"), str) for x in options)):
                    raise ProtocolError("invalid_question_options")
            if len(questions) == 1:
                value["options"] = [_option(x["label"], x["label"]) | {"description": x["description"]} for x in (questions[0].get("options") or [])]
        value["isBlocking"] = params.get("isBlocking")
    else:
        mode = params.get("mode")
        value["mode"] = mode
        value["serverName"] = params.get("serverName")
        schema = params.get("requestedSchema")
        if mode not in ("form", "openai/form", "openaiForm") or not _form_supported(schema):
            value.update(desktopRequired=True, question="Требуется защищённое подтверждение в Desktop", notice="unsupported_or_protected_elicitation")
        else:
            value["question"] = params.get("message", "")
            value["requestedSchema"] = deepcopy(schema)
            fields = schema["properties"]
            if len(fields) == 1:
                name, field = next(iter(fields.items()))
                for idx, enum in enumerate(field.get("enum", [])):
                    labels = field.get("enumNames")
                    value["options"].append(_option({"action": "accept", "content": {name: enum}}, labels[idx] if labels else enum))
            if not value["options"]:
                value["options"].append(_option({"action": "accept"}, "Отправить заполненную форму"))
            value["options"] += [_option({"action": a, "content": None}, label) for a, label in (("decline", "Отклонить"), ("cancel", "Отменить"))]
    value["key"] = _seal(value)
    return value


def validate_response(normalized, result):
    _check(normalized)
    if not isinstance(result, dict):
        raise ProtocolError("invalid_response")
    method = normalized["method"]
    if method in (COMMAND, FILE, LEGACY_COMMAND, LEGACY_FILE):
        if set(result) != {"decision"}:
            raise ProtocolError("invalid_decision_response")
        decision = result["decision"]
        offered = [x["value"] for x in normalized["options"]]
        denied = method in (LEGACY_COMMAND, LEGACY_FILE) and isinstance(decision, dict) and set(decision) == {"denied"} and isinstance(decision["denied"], dict) and set(decision["denied"]) == {"rejection"} and isinstance(decision["denied"]["rejection"], str)
        if not denied and not any(_same(decision, x) for x in offered):
            raise ProtocolError("decision_not_offered")
    elif method == PERMISSIONS:
        if set(result) - {"permissions", "scope", "strictAutoReview"} or "permissions" not in result or result.get("scope", "turn") not in ("turn", "session") or result.get("strictAutoReview") is not None and type(result["strictAutoReview"]) is not bool or not _subset(result["permissions"], normalized["permissions"]):
            raise ProtocolError("unrequested_or_invalid_permissions")
    elif method == INPUT:
        answers = result.get("answers")
        if set(result) != {"answers"} or not isinstance(answers, dict) or set(answers) != {q["id"] for q in normalized["questions"]}:
            raise ProtocolError("invalid_answer_questions")
        for answer in answers.values():
            if not isinstance(answer, dict) or set(answer) != {"answers"} or not isinstance(answer["answers"], list) or not answer["answers"] or any(not isinstance(x, str) or not x.strip() for x in answer["answers"]):
                raise ProtocolError("invalid_answer")
    else:
        if set(result) - {"action", "content"} or result.get("action") not in ("accept", "decline", "cancel"):
            raise ProtocolError("invalid_elicitation_response")
        if result["action"] == "accept":
            if not _form_content(normalized["requestedSchema"], result.get("content")):
                raise ProtocolError("invalid_form_content")
        elif result.get("content") is not None:
            raise ProtocolError("unexpected_elicitation_content")
    _json(result)
    return True


def _choice(normalized, choice):
    if type(choice) is int:
        if not 0 <= choice < len(normalized["options"]):
            raise ProtocolError("choice_out_of_range")
        return deepcopy(normalized["options"][choice]["value"])
    matches = [o["value"] for o in normalized["options"] if _same(o["value"], choice) or isinstance(choice, str) and o["label"] == choice]
    if len(matches) != 1:
        raise ProtocolError("choice_not_offered_or_ambiguous")
    return deepcopy(matches[0])


def build_response(normalized, choice=None, text=None, answers=None):
    _check(normalized)
    method = normalized["method"]
    selected = _choice(normalized, choice) if choice is not None else None
    if method in (COMMAND, FILE, LEGACY_COMMAND, LEGACY_FILE):
        if selected is None:
            raise ProtocolError("explicit_decision_required")
        if isinstance(selected, dict) and "denied" in selected and text is not None:
            selected["denied"]["rejection"] = text
        elif text is not None or answers is not None:
            raise ProtocolError("decision_does_not_accept_free_text")
        result = {"decision": selected}
    elif method == PERMISSIONS:
        if text is not None:
            raise ProtocolError("permissions_require_typed_response")
        result = deepcopy(answers if answers is not None else selected)
    elif method == INPUT:
        if answers is not None:
            if not isinstance(answers, dict):
                raise ProtocolError("invalid_answer_questions")
            result = {"answers": {k: {"answers": [v] if isinstance(v, str) else v} if not isinstance(v, dict) else deepcopy(v) for k, v in answers.items()}}
        elif len(normalized["questions"]) == 1:
            answer = text if text is not None else selected
            result = {"answers": {normalized["questions"][0]["id"]: {"answers": [answer]}}}
        else:
            raise ProtocolError("all_question_answers_required")
    else:
        if selected is None:
            raise ProtocolError("explicit_decision_required")
        result = selected
        if result["action"] == "accept" and "content" not in result:
            if answers is not None:
                result["content"] = deepcopy(answers)
            elif text is not None:
                props = normalized["requestedSchema"]["properties"]
                if len(props) == 1 and next(iter(props.values()))["type"] == "string":
                    result["content"] = {next(iter(props)): text}
                else:
                    try:
                        result["content"] = json.loads(text)
                    except (TypeError, ValueError):
                        raise ProtocolError("form_requires_typed_content") from None
        elif text is not None or answers is not None:
            raise ProtocolError("unexpected_answer_payload")
    validate_response(normalized, result)
    return result


def resolved_event(normalized, params, *, submitted=False):
    """A real server ACK can also be lifecycle cleanup; never invent receipt."""
    if not isinstance(normalized, dict) or normalized.get("key") != _seal(normalized):
        raise ProtocolError("stale_or_modified_request")
    if not isinstance(params, dict) or params.get("threadId") != normalized["threadId"] or typed_key(params.get("requestId")) != typed_key(normalized["requestId"]):
        raise ProtocolError("resolved_identity_mismatch")
    return {"key": normalized["key"], "state": "accepted_by_server" if submitted else "closed_without_local_answer"}

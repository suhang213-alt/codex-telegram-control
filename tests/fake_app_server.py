"""Artificial protocol peer. No model, credentials, filesystem tasks or network."""
import json
import sys

if "--version" in sys.argv:
    print("fake-codex 0.test")
    raise SystemExit(0)


def send(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    value = json.loads(line)
    method, request_id = value.get("method"), value.get("id")
    params = value.get("params", {})
    if request_id is None or method is None:
        continue
    if method == "initialize":
        result = {"userAgent": "synthetic-fixture"}
    elif method == "thread/start":
        if params.get("sandbox") not in ("read-only", "workspace-write"):
            send({"id": request_id, "error": {"code": -32602}})
            continue
        result = {"thread": {"id": "fixture-thread"}}
    elif method == "thread/resume":
        result = {"thread": {"id": params["threadId"]}}
    elif method == "model/list":
        result = {"data": [{"id": "fixture-model", "model": "fixture-model", "displayName": "Synthetic model", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]}
    elif method == "thread/read":
        result = {"thread": {"id": "fixture-thread", "turns": [{"items": [{"id": "history-item", "type": "agentMessage", "text": "Artificial saved reply"}]}]}}
    elif method == "turn/start":
        send({"method": "turn/started", "params": {"threadId": params["threadId"], "turn": {"id": "fixture-turn"}}})
        result = {"turn": {"id": "fixture-turn", "status": "inProgress"}}
        send({"id": request_id, "result": result})
        send({"method": "item/agentMessage/delta", "params": {"threadId": params["threadId"], "itemId": "fixture-item", "delta": "Artificial reply"}})
        send({"method": "turn/completed", "params": {"threadId": params["threadId"], "turn": {"id": "fixture-turn", "status": "completed"}}})
        continue
    elif method in ("turn/interrupt", "turn/steer"):
        result = {}
    else:
        send({"id": request_id, "error": {"code": -32601}})
        continue
    send({"id": request_id, "result": result})

"""Bounded delivery timing metadata, never message text, tokens or paths."""
import bridge_config as config
import argparse
import json
import os
from pathlib import Path
import re
import time

TRACE_PATH = config.runtime_dir() / "delivery_timing.jsonl"
STAGES = {"registered", "user_matched", "directory_event", "read_event", "final_detected", "delivery_start",
          "history_start", "history_done", "cache_start", "cache_done", "reply_start", "reply_done",
          "reply_cli_entered", "result_cached", "outbox_queued", "telegram_start", "telegram_ok",
          "telegram_uncertain", "ack_done"}

def mark(operation_id, stage, *, at=None, source_at=None):
    identifier = str(operation_id)
    if not re.fullmatch(r"[0-9]{1,24}", identifier) or stage not in STAGES:
        return
    row = {"operationId": identifier, "stage": stage, "at": time.time() if at is None else at}
    if source_at is not None:
        row["sourceAt"] = source_at
    if not all(type(v) in (int,float) for k,v in row.items() if k in ("at","sourceAt")):
        return
    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(TRACE_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode("ascii"))
        finally:
            os.close(fd)
    except OSError:
        pass  # Diagnostics must never change delivery outcome.

def read_operation(operation_id):
    identifier = str(operation_id)
    if not re.fullmatch(r"[0-9]{1,24}", identifier):
        raise ValueError("Numeric operation id required")
    if not TRACE_PATH.exists():
        return []
    rows=[]
    with TRACE_PATH.open(encoding="ascii") as stream:
        for line in stream:
            try:
                row=json.loads(line)
            except ValueError:
                continue
            if row.get("operationId")==identifier:
                rows.append(row)
    return rows

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("operation_id")
    args=parser.parse_args()
    print(json.dumps(read_operation(args.operation_id)))

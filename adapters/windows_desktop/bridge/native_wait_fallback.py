"""Pure conservative projection of a native read_thread head page.

No I/O, waiting, polling, mutations, invented source IDs or native cursors.
The caller must read the selected Codex thread with no pagination cursor,
only on initial reconciliation or a real external event.
"""
from __future__ import annotations

from copy import deepcopy


class SnapshotError(ValueError):
    """Fixed error codes only; never echo source data."""


def _identifier(value):
    return isinstance(value, str) and bool(value)


def snapshot_from_read_thread(data: dict, *, expected_thread_id: str,
                              expected_host_id: str | None = None) -> dict:
    """Project already-unwrapped native JSON to wait-compatible fields.

    This is a snapshot, not evidence that an owner command completed. Existing
    baseline/receipt/turn correlation must still run before notice or ACK.
    A final-looking assistant item never changes the native thread status.
    """
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise SnapshotError("snapshot_schema_invalid")
    thread = data.get("thread")
    if not isinstance(thread, dict) or thread.get("kind") != "codex":
        raise SnapshotError("snapshot_codex_required")
    if not _identifier(expected_thread_id) or thread.get("id") != expected_thread_id:
        raise SnapshotError("snapshot_target_mismatch")
    host = thread.get("hostId")
    if expected_host_id is not None and host != expected_host_id:
        raise SnapshotError("snapshot_host_mismatch")
    status = thread.get("status")
    if not isinstance(status, dict) or not _identifier(status.get("type")):
        raise SnapshotError("snapshot_status_missing")
    page = data.get("page")
    if not isinstance(page, dict) or page.get("order") != "newest_first":
        raise SnapshotError("snapshot_order_unverified")
    turns = data.get("turns")
    if not isinstance(turns, list):
        raise SnapshotError("snapshot_turns_invalid")
    selected = {"id": expected_thread_id, "status": deepcopy(status)}
    if host is not None:
        selected["hostId"] = host
    result = {
        "schemaVersion": 1, "thread": selected, "latestTurn": None,
        "latestAssistantMessageId": None, "latestToolMarkerId": None,
    }
    if not turns:
        return result
    turn = turns[0]
    if not isinstance(turn, dict) or not _identifier(turn.get("id")):
        raise SnapshotError("snapshot_turn_id_missing")
    if not _identifier(turn.get("status")) or not isinstance(turn.get("items"), list):
        raise SnapshotError("snapshot_turn_invalid")
    result["latestTurn"] = {
        key: deepcopy(turn.get(key))
        for key in ("id", "status", "error", "startedAt", "completedAt", "durationMs")
    }
    for item in turn["items"]:
        if (isinstance(item, dict) and item.get("type") == "agentMessage"
                and item.get("phase") in (None, "commentary", "final", "final_answer")
                and _identifier(item.get("id"))):
            result["latestAssistantMessageId"] = item["id"]
    # read_thread has no canonical latestToolMarker projection. Keep it absent;
    # caller can use exact structured choice evidence from the same native page.
    return result

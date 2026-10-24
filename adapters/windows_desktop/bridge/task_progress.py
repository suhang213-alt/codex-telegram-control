"""Bounded, read-only Desktop task status projection for an owner-authenticated route.

Authentication and Codex scope belong to the caller. This module reads exactly
one statically registered JSON; it does not execute commands or contact services.
"""
from __future__ import annotations
import bridge_config as config

import datetime as dt
import json
import math
from pathlib import Path
import re
from types import MappingProxyType

SCHEMA = "codex-task-progress/1"
THREAD_ID = ""
MAX_BYTES = 256 * 1024
STALE_AFTER_SECONDS = 120
_MSK = dt.timezone(dt.timedelta(hours=3))
_ROOT = Path(__file__).resolve().parents[2]
_TASKS = MappingProxyType({})
_METRICS = frozenset({"completed_items", "total_items"})
_ID = re.compile(r"[a-zA-Z0-9_.-]{1,80}\Z")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_STAGE_STATUS = frozenset({"pending", "in_progress", "completed", "waiting", "blocked"})
_ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)\Z")
_PRIVATE = re.compile(
    r"[a-zA-Z]:[\\/]|\\\\|/(?:Users|home|etc)/|"
    r"\b(?:api[_ -]?key|token|password|secret)\s*[:=]|"
    r"\bsk-[a-zA-Z0-9_-]{12,}|\b\d{6,}:[a-zA-Z0-9_-]{20,}|"
    r"\b(?:powershell|cmd\.exe|pythonw?(?:\.exe)?|curl|wget|invoke-webrequest|start-process)\s+",
    re.IGNORECASE)


def _clock(value=None):
    value = value if value is not None else dt.datetime.now(_MSK)
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_clock_required")
    return value.astimezone(_MSK)


def _text(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or
            (not empty and not value.strip()) or any(ord(c) < 32 for c in value) or
            _PRIVATE.search(value)):
        raise ValueError("invalid_display_text")
    return value


def _time(value):
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        raise ValueError("invalid_timestamp")
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(_MSK)
    except ValueError:
        raise ValueError("invalid_timestamp") from None


def _objects(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("nonfinite_json_number")


def _stage_number(value):
    if type(value) not in (int, float) or not 0 <= value <= 2**53 - 1 or not math.isfinite(value):
        raise ValueError("invalid_stage_number")
    return value


def _stage(point, projected):
    """Optional, explicit stage progress; never infer a business quantity."""
    status = point.get("status")
    if "status" in point:
        if not isinstance(status, str) or status not in _STAGE_STATUS:
            raise ValueError("invalid_stage_status")
        projected["status"] = status
    percent = _stage_number(point["percent"]) if "percent" in point else 100 if point["done"] else 0
    if percent > 100:
        raise ValueError("invalid_stage_percent")
    quantity = {"count_done", "count_total", "unit"}.intersection(point)
    if quantity:
        if quantity != {"count_done", "count_total", "unit"}:
            raise ValueError("incomplete_stage_quantity")
        count_done, count_total = _stage_number(point["count_done"]), _stage_number(point["count_total"])
        if count_total <= 0 or count_done > count_total:
            raise ValueError("invalid_stage_quantity")
        projected.update(count_done=count_done, count_total=count_total, unit=_text(point["unit"], 80))
        percent = count_done / count_total * 100
    if ((point["done"] and (percent != 100 or status is not None and status != "completed")) or
            (status == "completed" and (not point["done"] or percent != 100))):
        raise ValueError("inconsistent_stage_completion")
    if quantity or "percent" in point or "status" in point:
        projected["percent"] = percent
    return percent


def project(snapshot, thread_id=THREAD_ID, *, now=None):
    """Validate and project an in-memory snapshot; never read or modify files."""
    checked = _clock(now)
    if (type(snapshot) is not dict or not isinstance(thread_id, str) or not _UUID.fullmatch(thread_id) or
            snapshot.get("task_id") != thread_id or
            ("schema" in snapshot and snapshot["schema"] != "codex-task-status/1")):
        raise ValueError("invalid_snapshot_identity_or_schema")
    started = _time(snapshot.get("started_at"))
    updated = _time(snapshot.get("updated_at"))
    raw_finished = snapshot.get("finished_at")
    finished = _time(raw_finished) if raw_finished is not None else None
    if (started > updated or updated > checked + dt.timedelta(seconds=60) or
            (finished is not None and not started <= finished <= updated)):
        raise ValueError("inconsistent_timestamps")

    source_points = snapshot.get("checkpoints")
    if not isinstance(source_points, list) or not 1 <= len(source_points) <= 64:
        raise ValueError("invalid_checkpoints")
    checkpoints, seen, stage_percentages = [], set(), []
    for point in source_points:
        if not isinstance(point, dict):
            raise ValueError("invalid_checkpoint")
        identifier, done = point.get("id"), point.get("done")
        if (not isinstance(identifier, str) or not _ID.fullmatch(identifier) or
                identifier in seen or type(done) is not bool):
            raise ValueError("invalid_checkpoint")
        seen.add(identifier)
        projected = {"id": identifier, "title": _text(point.get("label"), 300), "done": done}
        stage_percentages.append(_stage(point, projected))
        checkpoints.append(projected)
    if (finished is not None and
            any(any(key in point for key in ("status", "percent", "count_done", "count_total", "unit"))
                for point in source_points) and
            not all(point["done"] and percent == 100 for point, percent in zip(checkpoints, stage_percentages))):
        raise ValueError("unfinished_stage_protocol")

    source_results = snapshot.get("lanes", [])
    if not isinstance(source_results, list) or len(source_results) > 32:
        raise ValueError("invalid_results")
    results = []
    for lane in source_results:
        if not isinstance(lane, dict):
            raise ValueError("invalid_result")
        results.append({"title": _text(lane.get("title"), 160),
                        "status": _text(lane.get("status"), 200),
                        "detail": _text(lane.get("detail"), 1200, empty=True)})

    source_metrics = snapshot.get("metrics", {})
    if not isinstance(source_metrics, dict):
        raise ValueError("invalid_metrics")
    metrics = {}
    for name in _METRICS.intersection(source_metrics):
        value = source_metrics[name]
        if (type(value) not in (int, float) or not 0 <= value <= 2**53 - 1 or
                not math.isfinite(value)):
            raise ValueError("invalid_metric")
        metrics[name] = value

    done = sum(p["done"] for p in checkpoints)
    total = len(checkpoints)
    age = max(0, int((checked - updated).total_seconds()))
    final = finished is not None
    stale = not final and age > STALE_AFTER_SECONDS
    return {
        "schema": SCHEMA, "available": True, "thread_id": thread_id,
        "title": _text(snapshot.get("task"), 160),
        "status": _text(snapshot.get("overall_status"), 160),
        "current": _text(snapshot.get("current"), 1200),
        "forecast": _text(snapshot.get("forecast", ""), 1200, empty=True),
        "started_at_msk": started.isoformat(), "updated_at_msk": updated.isoformat(),
        "finished_at_msk": finished.isoformat() if final else None,
        "checked_at_msk": checked.isoformat(), "final": final, "stale": stale,
        "running": not final and not stale, "age_seconds": age,
        "done": done, "total": total,
        # Preserve Desktop JS Math.round(done / total * 100), including x.5.
        "percent": math.floor(sum(stage_percentages) / total + 0.5),
        "checkpoints": checkpoints, "results": results, "metrics": metrics,
    }


def _unavailable(thread_id, reason, checked):
    return {"schema": SCHEMA, "available": False, "status": "unavailable",
            "thread_id": thread_id if isinstance(thread_id, str) and _UUID.fullmatch(thread_id) else None,
            "checked_at_msk": checked.isoformat(), "reason": reason}


def current(thread_id, *, now=None):
    """Read only the statically allowed task, after owner/scope auth in the caller.

    Unknown threads cause no filesystem access. Errors return generic reasons;
    filenames, paths, parser exceptions and source contents are never returned.
    """
    checked = _clock(now)
    binding = _TASKS.get(thread_id) if isinstance(thread_id, str) else None
    if binding is None:
        return _unavailable(thread_id, "unknown_thread", checked)
    path, task_id = binding
    if task_id != thread_id:
        return _unavailable(thread_id, "invalid_binding", checked)
    try:
        # The lexical registered path must not be redirected through a symlink.
        if path.resolve(strict=True) != path.absolute():
            return _unavailable(thread_id, "invalid_binding", checked)
        with path.open("rb") as handle:
            payload = handle.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError("snapshot_size_exceeded")
        snapshot = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_objects,
                              parse_constant=_invalid_constant)
        return project(snapshot, task_id, now=checked)
    except OSError:
        return _unavailable(thread_id, "source_unavailable", checked)
    except (ValueError, TypeError, RecursionError):
        return _unavailable(thread_id, "invalid_snapshot", checked)

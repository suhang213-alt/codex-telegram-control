"""Write one explicit local task phase; never run tasks or infer completion.

init creates s1..sN. update --result replaces the displayed result lanes when
provided. A completed phase is immutable until an explicit init archives it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import tempfile

from task_progress import MAX_BYTES, project
from local_path_safety import resolve_without_redirects

_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_MSK = dt.timezone(dt.timedelta(hours=3))


def _reject_redirect(path):
    for item in (path, *path.parents):
        try:
            # Cloud placeholders can be reparse points without redirecting the
            # pathname. The nearest existing ancestor also validates its parents.
            resolve_without_redirects(item)
            return
        except FileNotFoundError:
            continue


def _target(workspace, thread):
    if not _UUID.fullmatch(thread):
        raise ValueError("invalid_thread_uuid")
    raw = os.fspath(workspace)
    windows = PureWindowsPath(raw)
    if (raw.startswith(("\\\\", "//")) or windows.drive.startswith("\\\\") or
            (windows.drive and not windows.root)):
        raise ValueError("local_absolute_workspace_required")
    root = Path(raw)
    if not root.is_absolute() or ".." in root.parts or not root.is_dir():
        raise ValueError("existing_absolute_workspace_required")
    _reject_redirect(root)
    root = root.resolve(strict=True)
    target = root / ".task-progress" / (thread + ".json")
    _reject_redirect(target)
    if not target.resolve().is_relative_to(root):
        raise ValueError("path_outside_workspace")
    return target


def _objects(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _load(path, thread, now):
    _reject_redirect(path)
    with path.open("rb") as stream:
        payload = stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("snapshot_size_exceeded")
    snapshot = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_objects,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_number")))
    project(snapshot, thread, now=now)
    return snapshot, payload


def _atomic(path, payload):
    _reject_redirect(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_redirect(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.stem + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _reject_redirect(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _lanes(values):
    result = []
    for value in values:
        parts = value.split("|", 2)
        if len(parts) != 3:
            raise ValueError("result_requires_title_status_detail")
        result.append(dict(zip(("title", "status", "detail"), parts)))
    return result


def _stage_values(values, known):
    result = {}
    for value in values:
        parts = value.split("=", 1)
        if len(parts) != 2 or parts[0] not in known or parts[0] in result or not parts[1]:
            raise ValueError("invalid_stage_assignment")
        result[parts[0]] = parts[1]
    return result


def _number(value):
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 2**53 - 1:
        raise ValueError("invalid_stage_number")
    return int(number) if number.is_integer() else number


def _parser():
    parser = argparse.ArgumentParser(description="Локальное состояние текущей фазы задачи")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "update", "finish"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=True)
        command.add_argument("--thread", required=True)
        if name == "init":
            command.add_argument("--title", required=True)
            command.add_argument("--step", action="append", required=True)
        else:
            command.add_argument("--status")
            command.add_argument("--current")
            command.add_argument("--forecast")
            command.add_argument("--result", action="append")
        if name == "update":
            command.add_argument("--complete", action="append", default=[])
            command.add_argument("--pending", action="append", default=[])
            for field in ("percent", "count", "unit", "status"):
                command.add_argument("--stage-" + field, action="append", default=[])
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        path = _target(args.workspace, args.thread)
        now = dt.datetime.now(_MSK)
        timestamp = now.isoformat()
        previous = None
        if args.command == "init":
            snapshot = {
                "schema": "codex-task-status/1", "task_id": args.thread, "task": args.title,
                "started_at": timestamp, "updated_at": timestamp, "finished_at": None,
                "overall_status": "В работе", "current": args.step[0], "forecast": "",
                "checkpoints": [{"id": "s" + str(i), "label": label, "done": False}
                                for i, label in enumerate(args.step, 1)],
                "lanes": [], "metrics": {},
            }
            if path.exists():
                _, previous = _load(path, args.thread, now)
        else:
            snapshot, _ = _load(path, args.thread, now)
            if snapshot.get("finished_at") is not None:
                raise ValueError("new_phase_requires_init")
            if args.command == "update":
                complete, pending = set(args.complete), set(args.pending)
                known = {point["id"] for point in snapshot["checkpoints"]}
                if complete & pending or not (complete | pending).issubset(known):
                    raise ValueError("invalid_checkpoint_ids")
                assignments = {field: _stage_values(getattr(args, "stage_" + field), known)
                               for field in ("percent", "count", "unit", "status")}
                for point in snapshot["checkpoints"]:
                    identifier = point["id"]
                    if identifier in complete:
                        point["done"] = True
                    elif identifier in pending:
                        point["done"] = False
                    for field in ("percent", "unit", "status"):
                        if identifier in assignments[field]:
                            value = assignments[field][identifier]
                            point[field] = _number(value) if field == "percent" else value
                    if identifier in assignments["count"]:
                        parts = assignments["count"][identifier].split("/")
                        if len(parts) != 2:
                            raise ValueError("invalid_stage_count")
                        point["count_done"], point["count_total"] = map(_number, parts)
            elif not all(point["done"] for point in snapshot["checkpoints"]):
                raise ValueError("unfinished_checkpoints")
            if args.status is not None:
                snapshot["overall_status"] = args.status
            elif args.command == "finish":
                snapshot["overall_status"] = "Готово"
            for name in ("current", "forecast"):
                if getattr(args, name) is not None:
                    snapshot[name] = getattr(args, name)
            if args.result is not None:
                snapshot["lanes"] = _lanes(args.result)
            snapshot["updated_at"] = timestamp
            if args.command == "finish":
                snapshot["finished_at"] = timestamp
        view = project(snapshot, args.thread, now=now)
        payload = (json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > MAX_BYTES:
            raise ValueError("snapshot_size_exceeded")
        if previous is not None:
            archive = path.parent / "history" / (args.thread + "-" + now.strftime("%Y%m%dT%H%M%S%f%z") + ".json")
            _reject_redirect(archive)
            if archive.exists():
                raise ValueError("history_collision")
            _atomic(archive, previous)
        _atomic(path, payload)
        print(json.dumps({"task_id": args.thread, "done": view["done"], "total": view["total"],
                          "percent": view["percent"], "final": view["final"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, RecursionError):
        print("Срез не сохранён: проверьте путь, этапы и допустимость отображаемых данных.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

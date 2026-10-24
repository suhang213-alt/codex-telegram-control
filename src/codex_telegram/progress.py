"""Read only the selected chat's explicit progress snapshot. Never infer progress."""
from datetime import datetime, timezone
import json
import math
import re


def read_progress(workspace, identifier, minimum_started_at=None):
    missing = {"available": False, "reason": "No valid explicit snapshot for this chat."}
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
        return missing
    directory = workspace / ".task-progress"
    path = directory / (identifier + ".json")
    try:
        # macOS /var aliases and Windows short names can resolve differently
        # from the caller's spelling. Compare canonical paths on both sides.
        if directory.is_symlink() or path.is_symlink() or not path.resolve(strict=True).is_relative_to(workspace.resolve(strict=True)):
            return missing
        with path.open("rb") as stream:
            body = stream.read(262145)
        if len(body) > 262144:
            return missing
        value = json.loads(body)
        if value.get("schema") != "pbc-task-status/1" or value.get("task_id") != identifier:
            return missing
        points = value.get("checkpoints")
        if not isinstance(points, list) or not 1 <= len(points) <= 100:
            return missing
        stages = []
        for point in points:
            percent = point.get("percent", 100 if point.get("done") is True else 0)
            if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
                return missing
            done, total = point.get("count_done"), point.get("count_total")
            row = {"label": str(point.get("label", "Stage"))[:500], "done": point.get("done") is True,
                   "status": str(point.get("status", "unknown"))[:40], "percent": percent}
            if done is not None or total is not None:
                if type(done) not in (int, float) or type(total) not in (int, float) or not all(math.isfinite(x) for x in (done,total)) or not 0 <= done <= total or total <= 0:
                    return missing
                row.update(count_done=done,count_total=total,unit=str(point.get("unit", ""))[:60],percent=100*done/total)
            stages.append(row)
        updated = datetime.fromisoformat(value["updated_at"])
        if updated.tzinfo is None:
            return missing
        if minimum_started_at is not None:
            boundary = datetime.fromisoformat(minimum_started_at)
            started = datetime.fromisoformat(value["started_at"])
            if boundary.tzinfo is None or started.tzinfo is None or started < boundary or started > updated:
                return missing
        age = (datetime.now(timezone.utc)-updated).total_seconds()
        percent = sum(s["percent"] for s in stages)/len(stages)
        finished = value.get("finished_at")
        if finished is not None:
            ended = datetime.fromisoformat(finished)
            if ended.tzinfo is None or ended > updated:
                return missing
        return {"available": True, "percent": round(percent,1), "stages": stages,
                "updated_at": updated.isoformat(), "stale": age > 300 or age < -60,
                "complete": all(s["done"] and s["percent"] == 100 for s in stages) and finished is not None}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return missing

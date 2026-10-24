"""Reversible local transport switch; never reads Telegram credentials or state."""
from __future__ import annotations
import bridge_config as config

import json
from pathlib import Path
import sys
from typing import TextIO


MARKER_PATH = config.data_dir() / ".cloud_transport_enabled"
ACTIONS = {"cloud_transport_on", "cloud_transport_off", "cloud_transport_status"}


def is_enabled(*, marker_path: Path | None = None) -> bool:
    marker = MARKER_PATH if marker_path is None else marker_path
    try:
        return marker.exists()
    except OSError:
        # An unreadable switch must not reactivate polling or overwrite the menu.
        return True


def run_cli(action: str, *, marker_path: Path | None = None, stdout: TextIO | None = None) -> int:
    marker = MARKER_PATH if marker_path is None else marker_path
    output = sys.stdout if stdout is None else stdout
    try:
        if action not in ACTIONS:
            raise ValueError("Unsupported transport action")
        if action == "cloud_transport_on":
            marker.touch(exist_ok=True)
        elif action == "cloud_transport_off":
            marker.unlink(missing_ok=True)
        enabled = is_enabled(marker_path=marker)
        if action == "cloud_transport_on" and not enabled:
            raise OSError("Switch was not enabled")
        if action == "cloud_transport_off" and enabled:
            raise OSError("Switch was not disabled")
        result = {"ok": True, "cloud_transport": enabled, "polling_enabled": not enabled,
                  "local_menu_updates_enabled": True}
        code = 0
    except (Exception, KeyboardInterrupt):
        result = {"ok": False, "error": "cloud_transport_operation_failed"}
        code = 2
    print(json.dumps(result, separators=(",", ":")), file=output)
    return code

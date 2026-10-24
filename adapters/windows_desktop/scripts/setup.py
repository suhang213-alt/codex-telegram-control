"""Interactive setup. No subprocess, model call, or persistent OS setting changes."""
import getpass
import json
import os
from pathlib import Path
import re
import uuid
from _common import config, require_windows


def main():
    require_windows()
    print("Optional experimental Windows Desktop adapter. Close the portable bot first: one token must have one polling owner.")
    print("Desktop's private MCP protocol may change; doctor must pass before start. Approvals and sandbox remain enabled.")
    if input("Type ACCEPT to acknowledge this compatibility boundary: ").strip() != "ACCEPT":
        raise SystemExit("Setup cancelled")
    prior = config.settings()
    if prior and input("An adapter configuration already exists. Type RECONFIGURE to replace it: ").strip() != "RECONFIGURE":
        raise SystemExit("Setup cancelled")
    owner = int(input("Your Telegram numeric user ID (never username): ").strip())
    service = str(uuid.UUID(input("Dedicated existing local Desktop service chat UUID: ").strip()))
    workspace = Path(input("Allowed existing workspace directory: ").strip().strip('"')).expanduser().resolve(strict=True)
    codex = Path(input("Codex home directory [default ~/.codex]: ").strip().strip('"') or os.environ.get("CODEX_HOME", str(Path.home()/".codex"))).expanduser().resolve(strict=True)
    plugin = input("Installed bundled codex-app-tools directory (blank only if one version exists): ").strip().strip('"')
    value = {"schema":"codex-telegram-desktop/1", "owner_user_id":owner,
             "service_thread_id":service,"allowed_workspaces":[str(workspace)],
             "codex_home":str(codex),"plugin_dir":plugin,
             "compatibility_acknowledged":True,"tunnel_provider":"none"}
    print("Telegram's embedded panel requires public HTTPS. Optional localhost.run sends encrypted HTTPS traffic through a third-party tunnel.")
    if input("Enable the optional localhost.run tunnel? Type ENABLE (otherwise local only): ").strip() == "ENABLE":
        value["tunnel_provider"] = "localhost.run"
    config.validate(value)
    secret = getpass.getpass("BotFather token (hidden; stored with Windows user DPAPI): ").strip()
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", secret):
        raise SystemExit("Invalid token format")
    import telegram_bridge as bridge
    if bridge.STATE_PATH.is_file():
        state = bridge.read_json(bridge.STATE_PATH)
        if state.get("owner_user_id") not in (None, owner):
            raise SystemExit("Existing paired owner differs; uninstall local data explicitly before changing ownership")
    config.write_settings(value)
    bridge.SECRET_DIR.mkdir(parents=True, exist_ok=True)
    token_tmp = bridge.TOKEN_PATH.with_suffix(".tmp")
    token_tmp.write_bytes(bridge.protect(secret.encode("utf-8")))
    token_tmp.replace(bridge.TOKEN_PATH)
    secret = None
    print("Saved locally. Run doctor.py, then start.py. Pair using the one-time link printed by pair.py.")


if __name__ == "__main__":
    main()

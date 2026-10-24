"""Delete only this adapter's data after explicit confirmation; no OS settings touched."""
import argparse
import json
import os
from pathlib import Path
import shutil
from _common import config, require_windows
require_windows()
parser=argparse.ArgumentParser()
parser.add_argument("--delete-local-data",action="store_true")
args=parser.parse_args()
if not args.delete_local_data:
    print("Stop start.py with Ctrl+C, then delete the downloaded adapter folder. Local data is preserved.")
else:
    target=config.data_dir().resolve()
    if os.environ.get("CTD_DATA_DIR"):
        raise SystemExit("Custom CTD_DATA_DIR is never recursively removed. Stop the adapter and inspect/remove its files manually.")
    expected=(Path(os.environ["LOCALAPPDATA"])/"CodexTelegramDesktop").resolve()
    if target != expected or target.name != "CodexTelegramDesktop":
        raise SystemExit("Refusing nonstandard data directory")
    if target == Path(target.anchor) or target == Path.home().resolve() or target == config.codex_home().resolve():
        raise SystemExit("Refusing unsafe data directory")
    if not (target/"config.json").is_file() or json.loads((target/"config.json").read_text(encoding="utf-8")).get("schema") != "codex-telegram-desktop/1":
        raise SystemExit("Adapter ownership marker is missing; no files removed")
    print("This removes only adapter settings, DPAPI token, queue and cached attachments from:",target)
    if input("Stop the adapter first. Type DELETE ADAPTER DATA: ").strip() != "DELETE ADAPTER DATA":
        raise SystemExit("Cancelled")
    shutil.rmtree(target)
    print("Adapter local data removed. Codex and Telegram chats were not modified.")

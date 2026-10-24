"""Setup and operate the macOS adapter for existing Desktop chats."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .config import SafeError, atomic_json, load, secure_keyring, validate
from . import macos_platform as platform


def _desktop_config(home: Path) -> dict:
    config = load(home)
    if config.get("backend") != "desktop" or not isinstance(config.get("desktop"), dict):
        raise SafeError("This configuration is not an existing-Desktop adapter. Use a separate settings directory and Desktop setup.")
    return config


def _save_token(home: Path, secret: str) -> None:
    if not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,100}", secret):
        raise SafeError("Invalid bot token format. No credential was saved.")
    try:
        secure_keyring().set_password("codex-telegram-control", str(home.resolve()), secret)
    except SafeError:
        raise
    except Exception:
        raise SafeError("macOS Keychain could not save the bot token. Unlock it and retry.") from None


def setup(home: Path, *, app_bundle: str | None = None) -> None:
    platform.require_macos()
    if (home / "config.json").exists():
        raise SafeError("Configuration already exists. Setup does not replace it. Choose a separate CODEX_TELEGRAM_HOME for a new installation.")
    print("Existing Desktop setup. Your Desktop profile and login remain in the installed app.")
    bundle = app_bundle or input("Installed app bundle (Enter: discover Codex.app / ChatGPT.app): ").strip()
    identity = platform.discover_bundle(bundle or None)
    print(f"Verified OpenAI Desktop {identity['app_version']} ({identity['bundle_id']}; signing team {identity['team_id']}).")
    owner = input("Your numeric Telegram user ID (private chat ID): ").strip()
    workspace = input("Existing workspace whose Desktop chats may be controlled: ").strip()
    public = input("Your panel HTTPS origin (for example https://panel.example.org): ").strip()
    profile_value = input("Existing Desktop profile (Enter: ~/.codex): ").strip()
    profile = Path(profile_value or Path.home() / ".codex").expanduser()
    if not profile.is_absolute() or not profile.is_dir():
        raise SafeError("An existing Desktop profile is required. Open and sign in to Desktop normally first.")
    identity["codex_home"] = str(profile.resolve())
    try:
        config = validate({"schema": 1, "backend": "desktop", "owner_id": int(owner),
                           "workspace": workspace, "public_url": public, "port": 8787,
                           "sandbox": "read-only", "desktop": identity})
    except ValueError:
        raise SafeError("Telegram owner ID must be numeric.") from None
    work = Path(config["workspace"])
    if (work == Path.home().resolve() or work == Path(work.anchor)
            or home.resolve().is_relative_to(work) or profile.resolve().is_relative_to(work)):
        raise SafeError("Choose a dedicated workspace that does not contain the bridge settings, Desktop profile, or your home folder.")
    print("Desktop controls its existing sandbox and approval settings; this adapter does not override them.")
    secret = getpass.getpass("Your own BotFather token (hidden; Enter uses TELEGRAM_BOT_TOKEN at bridge runtime): ")
    home = platform.secure_home(home)
    if secret:
        _save_token(home, secret)
    atomic_json(home / "config.json", config)
    print("Saved locally. Close Desktop normally, then run: codex-telegram-desktop launch; codex-telegram-desktop doctor; codex-telegram-desktop run.")


def doctor(config: dict, home: Path) -> int:
    checks = {"platform": "macOS", "signature": "not_checked", "engine_hash": "not_checked",
              "desktop": "not_checked", "gateway": "not_connected", "telegram": "not_run",
              "model": "not_run", "credentials": "not_read", "existing_chat_roundtrip": "not_tested"}
    platform.verify_desktop(config)
    checks.update(signature="verified OpenAI", engine_hash="matches setup",
                  desktop="running" if platform.running_desktop(config) else "closed")
    try:
        from .desktop_rpc import DesktopRpc
        status = DesktopRpc.read_status(home)
        # Status contains only connection/capability evidence, never history.
        checks["gateway"] = "connected" if (isinstance(status, dict) and status.get("connected") is True
                                           and status.get("ready") is True
                                           and status.get("backend") == "desktop") else "not_connected"
    except (ImportError, SafeError, OSError, ValueError):
        pass
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["gateway"] == "connected" else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="codex-telegram-desktop",
                                     description="Control existing chats in the installed macOS Desktop app.")
    parser.add_argument("command", choices=("setup", "launch", "doctor", "run", "rollback", "set-token"))
    parser.add_argument("--app-bundle", help="setup only: explicit installed Codex.app or ChatGPT.app directory")
    args = parser.parse_args(argv)
    home = platform.desktop_home()
    try:
        platform.require_macos()
        if args.app_bundle and args.command != "setup":
            raise SafeError("--app-bundle is supported only by setup.")
        if args.command == "setup":
            setup(home, app_bundle=args.app_bundle)
            return 0
        config = _desktop_config(home)
        if args.command in {"launch", "rollback"}:
            outcome = platform.launch(config, home, rollback=args.command == "rollback")
            print(json.dumps(outcome, indent=2))
            if args.command == "launch":
                print("Desktop launch requested. Run doctor to verify the existing-engine connection before starting Telegram.")
            else:
                print("Original Desktop launch requested without the gateway override. Profile and application were not changed.")
        elif args.command == "doctor":
            return doctor(config, home)
        elif args.command == "set-token":
            from .cli import InstanceLock
            lock = InstanceLock(platform.secure_home(home))
            try:
                _save_token(home, getpass.getpass("New BotFather token (hidden; stored in Keychain): "))
            finally:
                lock.close()
            print("Bot token saved in macOS Keychain.")
        elif args.command == "run":
            platform.verify_desktop(config)
            from .desktop_controller import run
            run(config, home)
        return 0
    except SafeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Local operation failed. Check the installed app, permissions, and configuration; no private diagnostics were printed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

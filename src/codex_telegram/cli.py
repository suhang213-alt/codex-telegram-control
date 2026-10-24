"""Install, diagnose, authenticate and run without modifying the Desktop app."""
from __future__ import annotations
import argparse
import getpass
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import threading
from .config import SafeError, atomic_json, data_home, load, resolve_command, secure_keyring, token, validate
from .controller import Controller
from .rpc import Rpc
from .server import Server, handler_factory
from .telegram import Telegram


class InstanceLock:
    def __init__(self, home):
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = (home / "instance.lock").open("a+b")
        try:
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise SafeError("Another bridge is already using this data directory.") from None

    def close(self):
        self.stream.close()


def setup(home):
    print("Private Telegram / Codex setup. Existing Desktop chats and credentials are not imported.")
    if (home / "config.json").exists():
        raise SafeError("Configuration already exists. Use a separate CODEX_TELEGRAM_HOME for another installation.")
    owner = input("Your numeric Telegram user ID (private chat ID): ").strip()
    workspace = input("Absolute path of a dedicated work folder: ").strip().strip('"')
    public = input("Your panel's HTTPS origin (for example https://panel.example.org): ").strip()
    executable = input("Codex executable (Enter: codex on PATH): ").strip().strip('"') or "codex"
    mode = input("Allow edits inside the work folder? Type yes; Enter keeps read-only: ").strip().lower()
    try:
        config = validate({"schema": 1, "owner_id": int(owner), "workspace": workspace,
                           "public_url": public, "port": 8787, "codex_command": [executable],
                           "sandbox": "workspace-write" if mode == "yes" else "read-only"})
    except ValueError:
        raise SafeError("Telegram owner ID must be numeric.") from None
    resolve_command(config["codex_command"])
    work = Path(config["workspace"])
    if work == Path.home().resolve() or work == Path(work.anchor) or home.resolve().is_relative_to(work):
        raise SafeError("Use a dedicated work folder that does not contain the bridge settings or your home folder.")
    secret = getpass.getpass("Your BotFather token (hidden; Enter uses TELEGRAM_BOT_TOKEN at runtime): ")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if secret:
        import re
        if not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,100}", secret):
            raise SafeError("Token format is invalid. Nothing was saved.")
        try:
            secure_keyring().set_password("codex-telegram-control", str(home.resolve()), secret)
        except SafeError:
            raise
        except Exception:
            raise SafeError("Credential store failed. Nothing was saved; unlock your keychain and retry.") from None
    atomic_json(home / "config.json", config)
    (home / "codex").mkdir(mode=0o700, exist_ok=True)
    (Path(config["workspace"]) / "telegram-exports").mkdir(mode=0o700, exist_ok=True)
    print("Configuration saved locally. Next: codex-telegram login; codex-telegram doctor --protocol; codex-telegram run.")


def login(config, home):
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home / "codex")
    for key in ("TELEGRAM_BOT_TOKEN", "CODEX_CLI_PATH", "CODEX_THREAD_ID", "OPENAI_API_KEY", "CODEX_API_KEY",
                "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_MCP_NODE_PATH", "CTD_RELAY_KEY_FILE", "CTD_RELAY_PUBLIC_URL"):
        env.pop(key, None)
    result = subprocess.run([*resolve_command(config["codex_command"]), "login"], env=env,
                            cwd=config["workspace"], check=False)
    if result.returncode:
        raise SafeError("Codex login did not succeed. Follow the official local login instructions.")


def set_token(home):
    import re
    load(home)
    lock=InstanceLock(home)
    try:
        secret=getpass.getpass("New BotFather token (hidden; saved only in OS credential store): ")
        if not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,100}",secret):
            raise SafeError("Invalid token format. Existing credentials were not changed.")
        try:
            secure_keyring().set_password("codex-telegram-control",str(home.resolve()),secret)
        except SafeError:
            raise
        except Exception:
            raise SafeError("OS credential store could not save the token. Unlock it and retry.") from None
        print("Token saved in OS credential store. Start the bridge to use it.")
    finally:
        lock.close()


def doctor(config, home, protocol=False):
    command = resolve_command(config["codex_command"])
    checks = {"python": sys.version.split()[0], "os": platform.system(), "architecture": platform.machine(),
              "workspace": "exists", "credentials": "unavailable", "cli": "unverified",
              "protocol": "not_run", "telegram_network": "not_run", "real_mobile_device": "not_run"}
    try:
        token(home)
        checks["credentials"] = "available (value hidden)"
    except SafeError:
        pass
    env = dict(os.environ, CODEX_HOME=str(home / "codex"))
    for key in ("TELEGRAM_BOT_TOKEN", "CODEX_CLI_PATH", "CODEX_THREAD_ID", "OPENAI_API_KEY", "CODEX_API_KEY",
                "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_MCP_NODE_PATH", "CTD_RELAY_KEY_FILE", "CTD_RELAY_PUBLIC_URL"):
        env.pop(key, None)
    result = subprocess.run([*command, "--version"], env=env, capture_output=True, timeout=15, check=False)
    if result.returncode == 0:
        checks["cli"] = "version command passed"
    if protocol:
        rpc = Rpc([*command, "app-server", "--listen", "stdio://"], config["workspace"], home / "codex", lambda _: None)
        rpc.close()
        checks["protocol"] = "initialize passed; inference and native GUI integration not tested"
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if checks["cli"] == "unverified" or checks["credentials"] == "unavailable":
        return 2
    return 0


def run(config, home):
    secret = token(home)
    lock = InstanceLock(home)
    controller = None
    server = None
    telegram = None
    try:
        command = [*resolve_command(config["codex_command"]), "app-server", "--listen", "stdio://"]
        controller = Controller(config, home, lambda cb: Rpc(command, config["workspace"], home / "codex", cb))
        selected = controller.data.get("selected")
        if selected:
            try:
                controller.history(selected)
            except SafeError:
                controller.last_error = "Saved history unavailable; select a chat to retry the read."
        telegram = Telegram(secret, config["owner_id"], config["public_url"], controller)
        server = Server(("127.0.0.1", config["port"]), handler_factory(controller, telegram, secret, config))
        telegram.prepare()
        for target in (telegram.poll, telegram.notify):
            threading.Thread(target=target, daemon=True).start()
        print("Bridge is running. Open your bot's Codex menu. Stop with Ctrl+C. Keep this computer awake.")
        def request_stop(_signum, _frame):
            raise KeyboardInterrupt()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, request_stop)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping bridge. Interrupted or uncertain actions are not automatically resubmitted.")
    finally:
        if telegram:
            telegram.stop.set()
        if server:
            server.server_close()
        if controller:
            controller.close()
        lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="codex-telegram")
    parser.add_argument("command", choices=("setup", "login", "doctor", "run", "set-token", "forget-token"))
    parser.add_argument("--protocol", action="store_true", help="doctor: initialize app-server, no inference")
    args = parser.parse_args(argv)
    home = data_home()
    try:
        if args.command == "setup":
            setup(home)
        elif args.command == "set-token":
            set_token(home)
        elif args.command == "forget-token":
            try:
                secure_keyring().delete_password("codex-telegram-control", str(home.resolve()))
            except Exception:
                raise SafeError("Could not delete the token; check the OS credential store manually.") from None
            print("Stored bot token deleted. Revoke it in BotFather if it was exposed.")
        else:
            config = load(home)
            if config.get("backend")=="desktop":
                raise SafeError("This is a Desktop configuration. Use codex-telegram-desktop; a separate CLI engine was not started.")
            if args.command == "doctor":
                return doctor(config, home, args.protocol)
            if args.command == "login":
                login(config, home)
            else:
                run(config, home)
        return 0
    except SafeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Local operation failed. Check permissions, dependencies and configuration; no secret diagnostics were printed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

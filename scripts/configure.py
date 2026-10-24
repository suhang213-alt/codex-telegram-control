"""Change a local URL or sandbox mode without editing source code."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from codex_telegram.config import SafeError, atomic_json, data_home, load, validate
from codex_telegram.cli import InstanceLock


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--public-url")
    parser.add_argument("--desktop",action="store_true",help="Use the macOS Desktop bridge configuration")
    parser.add_argument("--sandbox",choices=("read-only","workspace-write"))
    args=parser.parse_args()
    if args.public_url is None and args.sandbox is None:
        parser.error("Specify --public-url or --sandbox")
    if args.desktop:
        from codex_telegram.macos_platform import desktop_home
        home=desktop_home()
    else:home=data_home()
    lock=InstanceLock(home)
    try:
        config=load(home)
        if args.sandbox is not None and config.get("backend")=="desktop":
            raise SafeError("Change this chat's permissions in Codex Desktop. The bridge does not override them.")
        if args.public_url is not None:config["public_url"]=args.public_url
        if args.sandbox is not None:config["sandbox"]=args.sandbox
        atomic_json(home/"config.json",validate(config))
        print("Configuration updated locally. Restart the bridge to apply it.")
    finally:lock.close()


if __name__=="__main__":
    try:main()
    except (SafeError,OSError,ValueError) as exc:
        print(str(exc) if isinstance(exc,SafeError) else "Local configuration update failed.",file=sys.stderr)
        raise SystemExit(2)

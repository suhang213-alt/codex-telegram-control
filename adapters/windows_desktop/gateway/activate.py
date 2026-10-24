"""Explicit Desktop activation with a process-local CLI override; no global edits."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from build import config, sha256


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--build",type=Path,default=config.data_dir()/"gateway-build")
    parser.add_argument("--rollback",action="store_true",help="Launch the installed Desktop without the shim override")
    args=parser.parse_args()
    if os.name!="nt":raise SystemExit("Windows required")
    from desktop_startup import WindowsStartupBackend
    backend=WindowsStartupBackend()
    target=backend.target()
    if backend.is_running(target):
        raise SystemExit("Desktop is running. Save work and exit it gracefully first; this command will not stop it.")
    desktop=Path(target.install_path)/"app/ChatGPT.exe"
    environment=dict(os.environ)
    environment.pop("CODEX_CLI_PATH",None)
    environment["CTD_DATA_DIR"]=str(config.data_dir())
    if not args.rollback:
        manifest=json.loads((args.build/"build-manifest.json").read_text(encoding="utf-8"))
        for field,path in (("config_sha256",args.build/"launcher-config.json"),("shim_sha256",args.build/"codex-gateway.exe"),("desktop_sha256",desktop)):
            if sha256(path)!=manifest[field]:raise SystemExit("Identity changed; rebuild the adapter before activation")
        environment["CODEX_CLI_PATH"]=str((args.build/"codex-gateway.exe").resolve())
    subprocess.Popen([str(desktop)],env=environment,creationflags=subprocess.CREATE_NO_WINDOW)
    print("Desktop launch requested. No model turn or approval was sent. Run doctor.py --native and perform an owner-controlled acceptance check.")


if __name__=="__main__":main()

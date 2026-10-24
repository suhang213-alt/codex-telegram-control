"""Run core tests with external networking and non-fixture child processes blocked."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--json",dest="report")
    args=parser.parse_args()
    repo=Path(__file__).resolve().parents[1]
    report=Path(args.report).resolve() if args.report else None
    attempts={"external_network":0,"unapproved_process":0}
    allowed=Path(__file__).resolve().parents[1]/"tests/fake_app_server.py"
    def guard(event,values):
        if event=="socket.connect":
            destination=values[1]
            if not isinstance(destination,tuple) or destination[0] not in ("127.0.0.1","::1"):
                attempts["external_network"]+=1
                raise RuntimeError("External network blocked by offline tests")
        if event=="socket.getaddrinfo" and values[0] not in ("127.0.0.1","::1","localhost",None):
            attempts["external_network"]+=1
            raise RuntimeError("External DNS blocked by offline tests")
        if event=="subprocess.Popen":
            command=values[1]
            expected=[sys.executable,"-B",str(allowed)]
            # Windows' audit event contains list2cmdline output; POSIX retains argv.
            if command != expected and command != subprocess.list2cmdline(expected):
                attempts["unapproved_process"]+=1
                raise RuntimeError("Only the artificial protocol peer may start")
    sys.dont_write_bytecode=True
    with tempfile.TemporaryDirectory(prefix="codex-telegram-offline-") as temp:
        temp_root=Path(temp)
        for key in ("CODEX_TELEGRAM_HOME","CODEX_HOME","LOCALAPPDATA","APPDATA","XDG_DATA_HOME"):
            destination=temp_root/key.lower();destination.mkdir()
            os.environ[key]=str(destination)
        for key in ("TELEGRAM_BOT_TOKEN","OPENAI_API_KEY","CODEX_API_KEY","CODEX_CLI_PATH","CODEX_THREAD_ID","CODEX_APP_TOOLS_PIPE_PATH","CODEX_MCP_NODE_PATH"):
            os.environ.pop(key,None)
        sys.addaudithook(guard)
        started=time.monotonic()
        suite=unittest.defaultTestLoader.discover(str(repo/"tests"),pattern="test_*.py")
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        receipt={"schema":"offline-core-checks/1","tests":result.testsRun,"failures":len(result.failures),
                 "errors":len(result.errors),"skipped":len(result.skipped),"elapsed_seconds":round(time.monotonic()-started,3),
                 "external_network_attempts":attempts["external_network"],"unapproved_process_attempts":attempts["unapproved_process"],
                 "synthetic_app_server":True,"live_telegram":False,"live_inference":False,
                 "passed":result.wasSuccessful() and not any(attempts.values())}
        if report:
            report.parent.mkdir(parents=True,exist_ok=True)
            report.write_text(json.dumps(receipt,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(receipt))
        return 0 if receipt["passed"] else 1


if __name__=="__main__":raise SystemExit(main())

"""Compatibility checks; network-free by default, native MCP only with --native."""
import argparse
import json
import os
from pathlib import Path
import sys
from _common import BRIDGE, config


def check(native=False):
    checks = []
    def add(name, ok, detail):
        checks.append({"check":name,"status":"PASS" if ok else "FAIL","detail":detail})
    add("platform", os.name == "nt", "Windows required for this adapter")
    add("python", sys.version_info[:2] == (3,12), "Python 3.12 required by the preserved Mini App HTTP implementation")
    try:
        config.validate(config.settings())
        add("configuration", True, "Owner, routing context and workspace allowlist validated")
    except Exception as error:
        add("configuration", False, type(error).__name__)
    add("native_plugin", (config.plugin_dir()/"server.mjs").is_file(), "An installed bundled plugin must be selected unambiguously")
    add("local_state", any(config.codex_home().glob("state_*.sqlite")), "Existing local Desktop metadata required")
    import sqlite3
    from contextlib import closing
    service_exists=False
    try:
        databases=sorted(config.codex_home().glob("state_*.sqlite"),key=lambda p:int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1)
        if databases:
            with closing(sqlite3.connect(databases[-1].as_uri()+"?mode=ro",uri=True)) as db:
                service_exists=db.execute("SELECT 1 FROM threads WHERE id=?",(config.service_thread_id(),)).fetchone() is not None
    except (sqlite3.Error,OSError,ValueError):
        pass
    add("service_context",service_exists,"The configured routing UUID must be an existing local Desktop chat")
    add("token", (config.data_dir()/"secrets/bot_token.dpapi").is_file(), "A local DPAPI token must be configured; token content not displayed")
    add("assets", (BRIDGE/"miniapp/index.html").is_file(), "Preserved Mini App assets")
    if config.settings().get("tunnel_provider") == "localhost.run":
        import shutil
        add("ssh", bool(shutil.which("ssh")), "OpenSSH must be installed to enable the optional HTTPS tunnel")
    if native and all(c["status"] == "PASS" for c in checks):
        client = None
        try:
            from native_discovery import discover_environment
            from native_mcp_client import NativeMcpClient
            environment = discover_environment(caller_thread_id=config.service_thread_id())
            client = NativeMcpClient(caller_thread_id=config.service_thread_id(), environment=environment)
            names = set(client.start())
            required = {"list_threads","read_thread","list_projects","send_message_to_thread"}
            add("desktop_protocol", required <= names, "Read-only handshake and tool catalog only; no model turn")
        except Exception as error:
            add("desktop_protocol", False, getattr(error,"code",type(error).__name__))
        finally:
            if client is not None:
                client.close()
    else:
        checks.append({"check":"desktop_protocol","status":"NOT_RUN","detail":"Run doctor.py --native with Desktop open; private API compatibility is not inferred"})
    return {"ok":all(c["status"] != "FAIL" for c in checks),"native_verified":native and any(c["check"]=="desktop_protocol" and c["status"]=="PASS" for c in checks),"checks":checks}


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--native",action="store_true")
    args=parser.parse_args()
    result=check(args.native)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result["ok"] else 2)

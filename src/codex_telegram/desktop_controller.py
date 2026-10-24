"""Use original Desktop chat IDs and permission settings through its own engine."""
from __future__ import annotations
from datetime import datetime,timezone
from pathlib import Path
import signal
import threading
import time
from .config import SafeError,token
from .controller import Controller,APPROVALS
from .desktop_rpc import DesktopRpc


class DesktopController(Controller):
    def __init__(self,config,home,rpc_factory=None):
        self._desktop_ready=False;self._early_events=[];self._refresh_lock=threading.Lock();self._last_refresh=0
        super().__init__(config,home,rpc_factory or (lambda callback:DesktopRpc(home,callback,start_events=False,bootstrap=False)))
        try:
            self.refresh_threads()
            if hasattr(self.rpc,"resync"):self.rpc.resync()
            with self.lock:
                self._desktop_ready=True
                early,self._early_events=self._early_events,[]
                for event in early:self.handle_event(event)
            selected=self.data.get("selected")
            if selected:self.history(selected)
            if hasattr(self.rpc,"start_events"):self.rpc.start_events()
        except Exception:
            self.close();raise

    def refresh_threads(self):
        if not self._refresh_lock.acquire(blocking=False):return
        try:
            rows={};cursor=None
            for _ in range(50):
                # The native default excludes appServer-created Desktop chats.
                params={"limit":100,"sourceKinds":["cli","vscode","appServer"]}
                if cursor:params["cursor"]=cursor
                result=self.rpc.call("thread/list",params)
                for row in result.get("data",[]):
                    identifier=row.get("id");cwd=row.get("cwd")
                    if not isinstance(identifier,str) or not isinstance(cwd,str):continue
                    candidate=Path(cwd).resolve()
                    if not candidate.is_relative_to(self.workspace):continue
                    if row.get("archived"):continue
                    old=self.data["threads"].get(identifier,{})
                    rows[identifier]={**old,"id":identifier,"title":str(row.get("name") or row.get("title") or row.get("preview") or "Codex Desktop")[:200],"cwd":str(candidate)}
                cursor=result.get("nextCursor")
                if not cursor:break
            else:raise SafeError("Too many Desktop chats. Select a narrower workspace.")
            with self.lock:
                self.data["threads"]=rows
                if self.data.get("selected") not in rows:self.data["selected"]=None
                self.pending={key:row for key,row in self.pending.items() if row["params"].get("threadId") in rows}
                self.active={key:value for key,value in self.active.items() if key in rows}
                self._save();self._last_refresh=time.monotonic()
        finally:self._refresh_lock.release()

    def _scope_params(self):
        # No cwd/approvalPolicy/sandbox override: preserve existing Desktop settings.
        return {}

    def _action(self,action,payload):
        if action=="new":raise SafeError("Create the chat in Codex Desktop, then refresh this panel.")
        if action=="refresh":
            self.refresh_threads();return {"refreshed":True}
        if action=="send" and payload.get("files"):
            # Files live in the explicitly configured workspace, never in the Desktop profile.
            self.require_thread(payload.get("thread_id") or self.data.get("selected"))
        return super()._action(action,payload)

    def handle_event(self,event):
        with self.lock:
            if not self._desktop_ready:
                if len(self._early_events)>=10000:raise SafeError("Desktop startup event backlog exceeded.")
                self._early_events.append(event);return
            method=event.get("method");params=event.get("params") or {};identifier=params.get("threadId")
            if method=="gateway/requestClaimed":
                request_id=params.get("requestId")
                self.pending={key:row for key,row in self.pending.items() if not (
                    type(row["id"]) is type(request_id) and row["id"]==request_id and row["params"].get("threadId")==identifier)}
                return
            if "id" in event and "method" in event:
                # We are an observer, never the owner of unsupported or foreign GUI requests.
                if identifier not in self.data["threads"] or method not in APPROVALS|{"item/tool/requestUserInput"}:return
                if any(type(row["id"]) is type(event["id"]) and row["id"]==event["id"] and row["params"].get("threadId")==identifier for row in self.pending.values()):return
                if not params.get("turnId") or params.get("turnId")!=self.active.get(identifier):return
                if method=="item/tool/requestUserInput" and any(q.get("isSecret") for q in params.get("questions",[])):
                    self._notify("A protected input is waiting in Codex Desktop. Enter it on the Mac.");return
            if method=="turn/started" and identifier in self.data["threads"]:
                self.loaded.add(identifier)
                # A new turn started directly in Desktop must not inherit old progress either.
                self.data["threads"][identifier]["phase_started_at"]=datetime.now(timezone.utc).isoformat()
                self._save()
        super().handle_event(event)

    def history(self,identifier):
        with self.lock:self.require_thread(identifier)
        result=self.rpc.call("thread/read",{"threadId":identifier,"includeTurns":True})
        items=[];active=None
        for turn in result.get("thread",{}).get("turns",[]):
            if turn.get("status")=="inProgress":active=turn.get("id")
            for item in turn.get("items",[]):
                if item.get("type")=="agentMessage":
                    items.append({"id":item.get("id"),"turn_id":turn.get("id"),"role":"assistant","text":str(item.get("text",""))[-100000:]})
                elif item.get("type")=="userMessage":
                    content="\n".join(x.get("text","") for x in item.get("content",[]) if x.get("type")=="text")
                    items.append({"id":item.get("id"),"role":"user","text":content[-100000:]})
        with self.lock:
            self.messages[identifier]=items[-100:]
            if active:
                self.active[identifier]=active;self.loaded.add(identifier);self.statuses[identifier]="inProgress"

    def snapshot(self):
        if self.connected and time.monotonic()-self._last_refresh>15:
            try:self.refresh_threads()
            except SafeError:self.last_error="Desktop chat list could not be refreshed; the last list is shown."
        result=super().snapshot()
        return dict(result,backend="desktop",mode="existing-desktop",sandbox="desktop-managed",capabilities={"new_chat":False,"existing_desktop_chats":True})


def run(config,home):
    from .cli import InstanceLock
    from .server import Server,handler_factory
    from .telegram import Telegram
    from . import relay_client
    secret=token(home);lock=InstanceLock(home);control=server=telegram=None
    try:
        control=DesktopController(config,home)
        if relay_client.enabled():
            if config["port"] != 8787: raise SafeError("Docker host adapter requires local port 8787.")
        telegram=Telegram(secret,config["owner_id"],config["public_url"],control)
        server=Server(("127.0.0.1",config["port"]),handler_factory(control,telegram,secret,config))
        if relay_client.enabled():
            relay_client.start(secret, config["owner_id"], config["public_url"], "macos").start()
        telegram.prepare()
        for target in (telegram.poll,telegram.notify):threading.Thread(target=target,daemon=True).start()
        print("Connected to existing Codex Desktop chats. Open your bot's Codex menu. Ctrl+C stops only the bridge.")
        if threading.current_thread() is threading.main_thread():
            def stop(_signal,_frame):raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM,stop)
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:print("Bridge stopped. Codex Desktop remains running.")
    finally:
        if relay_client.enabled(): relay_client.close()
        if telegram:telegram.stop.set()
        if server:server.server_close()
        if control:control.close()
        lock.close()

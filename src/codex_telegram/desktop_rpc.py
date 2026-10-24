"""Connect to the running Desktop-owned gateway. Never start an engine."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import uuid
from .config import SafeError

MAX_FRAME=8*1024*1024


def session_metadata(home):
    path=Path(home)/"native-session.json"
    try:
        if path.is_symlink() or path.parent.is_symlink():raise ValueError()
        info=path.stat()
        if info.st_size>16384 or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:raise ValueError()
        data=json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema")!="desktop-gateway/1":raise ValueError()
        if not re.fullmatch(r"[0-9a-f]{32}",data.get("epoch","")):raise ValueError()
        if not re.fullmatch(r"[0-9a-f]{64}",data.get("token","")):raise ValueError()
        endpoint=Path(data["socket"])
        if not endpoint.is_absolute() or endpoint.is_symlink() or endpoint.parent.is_symlink():raise ValueError()
        folder=endpoint.parent.stat();sock=endpoint.stat()
        if folder.st_uid!=os.getuid() or stat.S_IMODE(folder.st_mode)!=0o700:raise ValueError()
        if sock.st_uid!=os.getuid() or not stat.S_ISSOCK(sock.st_mode) or stat.S_IMODE(sock.st_mode)&0o077:raise ValueError()
        if endpoint.parent.parent.resolve()!=Path("/tmp").resolve() or not endpoint.parent.name.startswith("ctd-"):raise ValueError()
        return data
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        raise SafeError("Desktop gateway unavailable or unsafe. Start Desktop with codex-telegram-desktop launch, then run doctor.") from None


class DesktopRpc:
    """Small request client with no process ownership and no automatic replay."""
    def __init__(self,home,on_event,*,exchange=None,start_events=True,bootstrap=True):
        self.home=Path(home);self.on_event=on_event;self.closed=False
        self._metadata=None if exchange else session_metadata(home)
        self._exchange=exchange or self._socket_exchange
        self._stop=threading.Event();self._pending={};self._lock=threading.RLock()
        self._worker=None
        self.resync(deliver=bootstrap)
        if start_events:self.start_events()

    def resync(self,*,deliver=True):
        """Reconcile only before polling, after the workspace list established scope."""
        if self._worker is not None:raise SafeError("Stop polling before reconciling Desktop events.")
        status=self.request("status")
        if not status.get("ready") or not status.get("connected") or status.get("backend")!="desktop":
            raise SafeError("Desktop engine handshake is not ready. Open Desktop and retry doctor.")
        self.cursor=status.get("cursor",0)
        self._pending.clear()
        if not deliver:return
        for identifier,turn in status.get("active",{}).items():
            self._deliver({"method":"turn/started","params":{"threadId":identifier,"turn":{"id":turn}}})
        for event in status.get("contexts",[]):self._deliver(event)
        for event in status.get("pending",[]):
            context=event.get("contextItem")
            if context:
                params=event.get("params",{})
                self._deliver({"method":"item/started","params":{"threadId":params.get("threadId"),"turnId":params.get("turnId"),"item":context}})
            self._deliver({key:value for key,value in event.items() if key!="contextItem"})

    def start_events(self):
        if self._worker is None and not self.closed:
            self._worker=threading.Thread(target=self._poll,daemon=True);self._worker.start()

    @classmethod
    def read_status(cls,home):
        instance=object.__new__(cls)
        instance._metadata=session_metadata(home)
        return instance._socket_exchange({"command":"status","operation_id":uuid.uuid4().hex})

    def _socket_exchange(self,message):
        request=dict(message,token=self._metadata["token"],epoch=self._metadata["epoch"])
        raw=(json.dumps(request,ensure_ascii=False,separators=(",",":"))+"\n").encode()
        if len(raw)>MAX_FRAME:raise SafeError("Desktop request is too large.")
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(36)
                connection.connect(self._metadata["socket"])
                connection.sendall(raw)
                with connection.makefile("rb") as stream:answer=stream.readline(MAX_FRAME+1)
            if len(answer)>MAX_FRAME or not answer.endswith(b"\n"):raise ValueError()
            response=json.loads(answer)
            if response.get("ok") is not True:
                raise SafeError("Desktop rejected the request or its outcome is unknown. Check the original chat before retrying.")
            return response["result"]
        except (OSError,ValueError,KeyError,TypeError):
            raise SafeError("Desktop connection lost; operation outcome may be unknown. No automatic resend was made.") from None

    def request(self,command,**payload):
        if self.closed:raise SafeError("Desktop gateway connection is closed.")
        return self._exchange(dict(payload,command=command,operation_id=uuid.uuid4().hex))

    def call(self,method,params,**_kwargs):
        if method not in {"thread/list","thread/read","thread/resume","model/list","turn/start","turn/steer","turn/interrupt"}:
            raise SafeError("This action is unavailable for the connected Desktop.")
        return self.request("rpc",method=method,params=params)

    @staticmethod
    def _key(identifier):return type(identifier).__name__+":"+str(identifier)

    def reply(self,identifier,result=None,error=None):
        if error is not None:raise SafeError("Desktop confirmations must stay available in the original client.")
        with self._lock:
            event=self._pending.get(self._key(identifier))
            if event is None:raise SafeError("Original Desktop request is no longer pending.")
            params=event["params"]
            # Reserve before the network write; never replay a possibly delivered answer.
            del self._pending[self._key(identifier)]
        return self.request("reply",request_id=identifier,thread_id=params["threadId"],turn_id=params["turnId"],result=result)

    def _deliver(self,event):
        if not isinstance(event,dict):raise SafeError("Invalid Desktop event.")
        with self._lock:
            if "id" in event and "method" in event:self._pending[self._key(event["id"])]=event
            if event.get("method") in {"serverRequest/resolved","gateway/requestClaimed"}:
                self._pending.pop(self._key(event.get("params",{}).get("requestId")),None)
        self.on_event(event)

    def _poll(self):
        try:
            while not self._stop.is_set():
                batch=self.request("events",after=self.cursor,timeout=3)
                if batch.get("gap"):raise SafeError("Desktop event history overflowed; reconnect to reconcile.")
                if not batch.get("connected") or not batch.get("ready") or batch.get("ambiguous"):
                    raise SafeError("Desktop engine is unavailable or ambiguous; reconnect after inspection.")
                for row in batch.get("events",[]):
                    self._deliver(row["event"])
                self.cursor=batch["cursor"]
                self._stop.wait(.05)
        except Exception:
            if not self._stop.is_set():self.on_event({"method":"bridge/disconnected","params":{}})
        finally:self.closed=True

    def close(self):
        self._stop.set();self.closed=True
        # Desktop and its engine remain running. Only our client worker stops.
        if self._worker and self._worker is not threading.current_thread():self._worker.join(4)

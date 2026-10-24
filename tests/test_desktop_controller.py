import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from codex_telegram.config import SafeError,validate
from codex_telegram.desktop_controller import DesktopController
from codex_telegram.desktop_rpc import DesktopRpc
from codex_telegram.desktop_gateway import GatewayCore,wire
from codex_telegram.telegram import Telegram


class DesktopFake:
    def __init__(self,workspace):
        self.workspace=workspace;self.calls=[];self.replies=[];self.closed=False;self.fail_send=False
    def call(self,method,params,**kwargs):
        self.calls.append((method,params))
        if method=="thread/list":return {"data":[{"id":"existing-chat","name":"Existing Desktop fixture","cwd":str(self.workspace)},
            {"id":"outside-chat","name":"Private other project","cwd":str(self.workspace.parent/"other")}],"nextCursor":None}
        if method=="thread/read":return {"thread":{"id":"existing-chat","cwd":str(self.workspace),"turns":[{"id":"past-turn","status":"completed","items":[
            {"id":"past-user","type":"userMessage","content":[{"type":"text","text":"Prior Desktop question"}]},
            {"id":"past-agent","type":"agentMessage","text":"Prior Desktop answer"},
            {"type":"reasoning","text":"Never expose this"}]}]}}
        if method=="turn/start":
            if self.fail_send:raise SafeError("Uncertain fixture outcome")
            return {"turn":{"id":"live-turn","status":"inProgress"}}
        if method=="model/list":return {"data":[]}
        return {}
    def reply(self,identifier,result=None,error=None):self.replies.append((identifier,result,error))
    def close(self):self.closed=True


class DesktopControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.workspace=self.root/"work";self.workspace.mkdir()
        self.home=self.root/"private";self.home.mkdir()
        self.config=validate({"schema":1,"owner_id":4242,"workspace":str(self.workspace),"public_url":"https://panel.example.org","backend":"desktop"})
        self.rpc=DesktopFake(self.workspace)
        self.controller=DesktopController(self.config,self.home,lambda callback:self.rpc)
        self.assertIsNone(self.controller.snapshot()["selected"])
        self.assertNotIn("thread/read",[m for m,p in self.rpc.calls])
        self.controller.execute("select_initial_1","select",{"thread_id":"existing-chat"})
    def tearDown(self):self.controller.close();self.temp.cleanup()
    def test_existing_ids_and_history_without_new_server_or_thread(self):
        state=self.controller.snapshot()
        self.assertEqual(self.rpc.calls[0],("thread/list",{"limit":100,"sourceKinds":["cli","vscode","appServer"]}))
        self.assertEqual([x["id"] for x in state["threads"]],["existing-chat"])
        self.assertEqual(state["selected"],"existing-chat")
        self.assertEqual([x["text"] for x in state["messages"]],["Prior Desktop question","Prior Desktop answer"])
        self.assertEqual(state["sandbox"],"desktop-managed")
        self.assertEqual(state["mode"],"existing-desktop")
        self.assertFalse(state["capabilities"]["new_chat"])
        self.assertNotIn("initialize",[m for m,p in self.rpc.calls])
        with self.assertRaises(SafeError):self.controller.execute("new_request_1","new",{"title":"No duplicate"})
    def test_send_keeps_original_id_and_native_permissions(self):
        self.controller.execute("send_request_1","send",{"text":"Continue original"})
        resumed=next(p for m,p in self.rpc.calls if m=="thread/resume")
        self.assertEqual(resumed,{"threadId":"existing-chat"})
        sent=next(p for m,p in self.rpc.calls if m=="turn/start")
        self.assertEqual(sent["threadId"],"existing-chat")
        self.assertFalse(set(sent)&{"cwd","sandbox","approvalPolicy"})
        self.assertNotIn("thread/start",[m for m,p in self.rpc.calls])
        before=len(self.rpc.calls)
        self.controller.execute("send_request_1","send",{"text":"Continue original"})
        self.assertEqual(len(self.rpc.calls),before)
    def test_unknown_delivery_never_replays(self):
        self.rpc.fail_send=True
        with self.assertRaises(SafeError):self.controller.execute("unknown_send_1","send",{"text":"One time"})
        before=len(self.rpc.calls)
        with self.assertRaises(SafeError):self.controller.execute("unknown_send_1","send",{"text":"One time"})
        self.assertEqual(len(self.rpc.calls),before)
    def test_outside_chat_cannot_send(self):
        with self.assertRaises(SafeError):self.controller.execute("outside_send_1","send",{"thread_id":"outside-chat","text":"Blocked"})
        self.assertNotIn("turn/start",[m for m,p in self.rpc.calls])
    def test_foreign_unknown_and_secret_gui_requests_are_not_rejected(self):
        self.controller.handle_event({"method":"turn/started","params":{"threadId":"existing-chat","turn":{"id":"live-turn"}}})
        for method,identifier,extra in [("future/request","existing-chat",{}),("item/tool/requestUserInput","outside-chat",{}),
            ("item/tool/requestUserInput","existing-chat",{"questions":[{"id":"secret","isSecret":True}]})]:
            self.controller.handle_event({"id":"native-request","method":method,"params":{"threadId":identifier,"turnId":"live-turn",**extra}})
        self.assertEqual(self.rpc.replies,[]);self.assertEqual(self.controller.pending,{})
    def test_desktop_winner_closes_card_without_false_server_ack(self):
        self.controller.handle_event({"method":"turn/started","params":{"threadId":"existing-chat","turn":{"id":"live-turn"}}})
        self.controller.handle_event({"id":7,"method":"item/commandExecution/requestApproval","params":{"threadId":"existing-chat","turnId":"live-turn","command":"echo synthetic"}})
        self.assertEqual(len(self.controller.pending),1)
        self.controller.handle_event({"method":"gateway/requestClaimed","params":{"requestId":7,"threadId":"existing-chat","turnId":"live-turn","by":"desktop"}})
        self.assertFalse(self.controller.pending);self.assertFalse(self.rpc.replies)
    def test_stop_uses_exact_desktop_turn_and_close_does_not_interrupt(self):
        self.controller.handle_event({"method":"turn/started","params":{"threadId":"existing-chat","turn":{"id":"live-turn"}}})
        self.controller.execute("stop_request_1","stop",{})
        self.assertEqual(self.rpc.calls[-1],("turn/interrupt",{"threadId":"existing-chat","turnId":"live-turn"}))
        before=len(self.rpc.calls);self.controller.close()
        self.assertEqual(len(self.rpc.calls),before)


class DesktopRpcTests(unittest.TestCase):
    def test_disconnect_and_ambiguity_clear_panel_state(self):
        events=[]
        def exchange(request):
            if request["command"]=="status":return {"backend":"desktop","ready":True,"connected":True,"cursor":0}
            return {"ready":False,"connected":True,"ambiguous":True,"cursor":0,"events":[]}
        client=DesktopRpc(Path("unused"),events.append,exchange=exchange,start_events=False)
        client._poll()
        self.assertTrue(client.closed)
        self.assertEqual(events,[{"method":"bridge/disconnected","params":{}}])

    def test_attach_no_initialize_and_original_decision_identity(self):
        calls=[];events=[]
        def exchange(request):
            calls.append(request)
            if request["command"]=="status":return {"backend":"desktop","ready":True,"connected":True,"cursor":4,"active":{"existing-chat":"native-turn"},"pending":[
                {"id":23,"method":"item/tool/requestUserInput","params":{"threadId":"existing-chat","turnId":"native-turn","questions":[{"id":"q","question":"Continue?"}]}}]}
            return {"accepted":True}
        client=DesktopRpc(Path("unused"),events.append,exchange=exchange,start_events=False)
        client.reply(23,{"answers":{"q":{"answers":["Yes"]}}})
        self.assertEqual(calls[-1]["request_id"],23)
        self.assertEqual(calls[-1]["thread_id"],"existing-chat")
        self.assertEqual(calls[-1]["turn_id"],"native-turn")
        with self.assertRaises(SafeError):client.reply(23,{})
        with self.assertRaises(SafeError):client.call("initialize",{})
        before=len(calls);client.close();self.assertEqual(len(calls),before)
        self.assertEqual([row["command"] for row in calls],["status","reply"])
    def test_nonready_gateway_refuses(self):
        with self.assertRaises(SafeError):DesktopRpc(Path("unused"),lambda event:None,exchange=lambda request:{"ready":False},start_events=False)
        with self.assertRaises(SafeError):DesktopRpc(Path("unused"),lambda event:None,exchange=lambda request:{"ready":True,"connected":True,"backend":"isolated"},start_events=False)


class ExistingDesktopPipelineTests(unittest.TestCase):
    def test_attach_recovers_original_pending_before_scope_without_new_engine(self):
        with tempfile.TemporaryDirectory() as temp, patch("subprocess.Popen") as process, patch("socket.socket") as network:
            work=Path(temp)/"work";work.mkdir();home=Path(temp)/"private";home.mkdir()
            engine=[];gui=[]
            def write_engine(raw):
                row=json.loads(raw);engine.append(row)
                if not isinstance(row.get("id"),str) or not row["id"].startswith("ctd-"):return
                method=row.get("method");params=row.get("params",{})
                if method=="thread/list":result={"data":[{"id":"existing-chat","cwd":str(work),"name":"Existing fixture"}]}
                elif method=="thread/read":result={"thread":{"id":"existing-chat","cwd":str(work),"turns":[{"id":"native-turn","status":"inProgress","items":[{"type":"agentMessage","text":"Original Desktop history"}]}]}}
                else:result={}
                core.from_engine(wire({"id":row["id"],"result":result}))
            core=GatewayCore({"workspace":str(work)},write_engine,gui.append)
            core.from_gui(wire({"id":1,"method":"initialize","params":{}}))
            core.from_engine(wire({"id":1,"result":{}}));core.from_gui(wire({"method":"initialized"}))
            core.from_engine(wire({"method":"turn/started","params":{"threadId":"existing-chat","turn":{"id":"native-turn"}}}))
            original={"id":41,"method":"item/tool/requestUserInput","params":{"threadId":"existing-chat","turnId":"native-turn","questions":[{"id":"choice","question":"Continue?","options":[{"label":"Yes"}],"isOther":False}]}}
            core.from_engine(wire(original))
            self.assertEqual(core.status()["pending"],[])
            def factory(callback):
                return DesktopRpc(home,callback,exchange=lambda request:core.command(dict(request,epoch=core.epoch)),start_events=False,bootstrap=False)
            config=validate({"schema":1,"owner_id":4242,"workspace":str(work),"public_url":"https://panel.example.org","backend":"desktop"})
            control=DesktopController(config,home,factory)
            try:
                deadline=time.monotonic()+3
                while not control.pending and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual(len(control.pending),1)
                self.assertIsNone(control.snapshot()["selected"])
                control.execute("select_existing_1","select",{"thread_id":"existing-chat"})
                state=control.snapshot()
                self.assertEqual(state["messages"][0]["text"],"Original Desktop history")
                self.assertEqual(len(state["pending"]),1)
                control.execute("native_decision_1","decision",{"thread_id":"existing-chat","key":state["pending"][0]["key"],"answers":{"choice":"Yes"}})
                self.assertIn({"id":41,"result":{"answers":{"choice":{"answers":["Yes"]}}}},engine)
                self.assertEqual(sum(row.get("method")=="initialize" for row in engine),1)
                self.assertFalse(any(row.get("method")=="thread/start" for row in engine))
                self.assertIn(wire(original),gui)
            finally:control.close();core.close()
            process.assert_not_called();network.assert_not_called()


if __name__=="__main__":unittest.main()

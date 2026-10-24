import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_telegram.config import SafeError, validate, data_home, atomic_json, resolve_command
from codex_telegram.security import authenticate, owned_message
from codex_telegram.controller import Controller
from codex_telegram.rpc import Rpc
from codex_telegram.server import Server, handler_factory
from codex_telegram.telegram import Telegram
from codex_telegram.cli import InstanceLock
from codex_telegram.progress import read_progress
from datetime import datetime,timezone,timedelta

TEST_TOKEN = "synthetic-test-key-not-a-telegram-token"
OWNER = 4242


def signed(owner=OWNER, stamp=None, **extras):
    data = {"auth_date": str(int(time.time()) if stamp is None else stamp), "user": json.dumps({"id": owner}), **extras}
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, "\n".join(f"{key}={data[key]}" for key in sorted(data)).encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(dict(data, hash=digest))


class FakeRpc:
    def __init__(self, callback):
        self.callback, self.calls, self.replies = callback, [], []
        self.fail_reply = False
    def call(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "test-thread"}}
        if method == "model/list":
            return {"data": [{"id": "test-model", "model": "test-model", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]}
        if method == "thread/read":
            return {"thread": {"turns": [{"items": [{"type": "agentMessage", "id": "old", "text": "Saved artificial result"}, {"type": "reasoning", "text": "must not render"}]}]}}
        return {}
    def reply(self, identifier, result=None, error=None):
        self.replies.append((identifier, result, error))
        if self.fail_reply:
            raise SafeError("Uncertain write")
    def close(self):
        pass


class SecurityTests(unittest.TestCase):
    def test_signed_owner_only(self):
        self.assertEqual(authenticate(signed(), TEST_TOKEN, OWNER), OWNER)
        with self.assertRaises(SafeError):
            authenticate(signed(owner=4343), TEST_TOKEN, OWNER)
    def test_expired_future_tampered_duplicate(self):
        cases = [signed(stamp=1), signed(stamp=int(time.time())+300), signed()+"&user=other", signed().replace("4242", "4243")]
        for value in cases:
            with self.subTest(value=value[:20]), self.assertRaises((SafeError, ValueError)):
                authenticate(value, TEST_TOKEN, OWNER)
    def test_private_chat_and_numeric_owner(self):
        self.assertTrue(owned_message({"from":{"id":OWNER},"chat":{"id":OWNER,"type":"private"}}, OWNER))
        self.assertFalse(owned_message({"from":{"id":OWNER},"chat":{"id":OWNER,"type":"group"}}, OWNER))
        self.assertFalse(owned_message({"from":{"id":"4242"},"chat":{"id":OWNER,"type":"private"}}, OWNER))
    def test_os_data_paths(self):
        fixture_home = Path(tempfile.gettempdir())/"synthetic-home"
        with patch.dict(os.environ, {}, clear=True), patch.object(Path,"home",return_value=fixture_home):
            with patch("sys.platform", "darwin"):
                self.assertEqual(data_home(), Path.home()/"Library/Application Support/codex-telegram-control")
            with patch("sys.platform", "linux"):
                self.assertEqual(data_home(), Path.home()/".local/share/codex-telegram-control")
            with patch("sys.platform", "win32"):
                self.assertEqual(data_home(), Path.home()/"AppData/Local/codex-telegram-control")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ctc test spaces ")
        self.root = Path(self.temp.name)
        self.work = self.root/"work"; self.work.mkdir()
        self.home = self.root/"private"; self.home.mkdir()
        self.config = validate({"schema":1,"owner_id":OWNER,"workspace":str(self.work),"public_url":"https://panel.example.org","sandbox":"read-only"})
        self.c = Controller(self.config, self.home, FakeRpc)
    def tearDown(self):
        self.c.close(); self.temp.cleanup()
    def new(self):
        return self.c.execute("request_new_1", "new", {"title":"Artificial chat"})
    def approval(self, method="item/commandExecution/requestApproval", identifier=17, **params):
        self.new(); self.c.active["test-thread"]="test-turn"
        self.c.handle_event({"id":identifier,"method":method,"params":{"threadId":"test-thread","turnId":"test-turn","itemId":"tool-1","command":"echo artificial",**params}})
        return next(iter(self.c.pending))
    def test_default_and_sandbox_not_weakened(self):
        self.new()
        self.assertEqual(self.c.rpc.calls[-1][1]["sandbox"],"read-only")
        self.assertEqual(self.c.rpc.calls[-1][1]["approvalPolicy"],"on-request")
        with self.assertRaises(SafeError): validate(dict(self.config,sandbox="danger-full-access"))
    def test_creation_is_idempotent(self):
        first=self.new(); self.assertEqual(first,self.new())
        self.assertEqual(len(self.c.rpc.calls),1)
        with self.assertRaises(SafeError): self.c.execute("request_new_1","new",{"title":"Different"})
    def test_unknown_is_not_success_or_retried(self):
        with self.assertRaises(SafeError): self.c.execute("request_failed_1","missing",{})
        with self.assertRaises(SafeError): self.c.execute("request_failed_1","missing",{})
        self.assertEqual(self.c.snapshot()["uncertain_operations"],1)
    def test_only_own_threads(self):
        self.new()
        with self.assertRaises(SafeError): self.c.execute("request_send_other","send",{"thread_id":"someone-else","text":"test"})
        self.assertEqual(len(self.c.rpc.calls),1)
    def test_steer_uses_current_turn(self):
        self.new(); self.c.active["test-thread"]="turn-current"
        self.c.execute("request_send_active","send",{"text":"Synthetic steering"})
        self.assertEqual(self.c.rpc.calls[-1][0],"turn/steer")
        self.assertEqual(self.c.rpc.calls[-1][1]["expectedTurnId"],"turn-current")
    def test_approval_is_explicit_single_and_scoped(self):
        key=self.approval()
        self.assertEqual(self.c.rpc.replies,[])
        self.c.answer("test-thread",{"key":key,"decision":"accept"})
        self.assertEqual(self.c.rpc.replies[-1][1],{"decision":"accept"})
        with self.assertRaises(SafeError): self.c.answer("test-thread",{"key":key,"decision":"accept"})
    def test_no_repeat_after_uncertain_approval(self):
        key=self.approval(); self.c.rpc.fail_reply=True
        with self.assertRaises(SafeError): self.c.answer("test-thread",{"key":key,"decision":"accept"})
        with self.assertRaises(SafeError): self.c.answer("test-thread",{"key":key,"decision":"accept"})
        self.assertEqual(len(self.c.rpc.replies),1)
    def test_resolution_requires_typed_id_and_thread(self):
        key=self.approval(identifier=1)
        for identifier,thread in [("1","test-thread"),(True,"test-thread"),(1,"other")]:
            self.c.handle_event({"method":"serverRequest/resolved","params":{"requestId":identifier,"threadId":thread}})
            self.assertIn(key,self.c.pending)
        self.c.handle_event({"method":"serverRequest/resolved","params":{"requestId":1,"threadId":"test-thread"}})
        self.assertNotIn(key,self.c.pending)
    def test_missing_diff_disables_accept(self):
        key=self.approval(method="item/fileChange/requestApproval",command=None)
        with self.assertRaises(SafeError): self.c.answer("test-thread",{"key":key,"decision":"accept"})
        self.c.answer("test-thread",{"key":key,"decision":"decline"})
    def test_full_diff_review_context(self):
        self.new();self.c.active["test-thread"]="test-turn"
        self.c.handle_event({"method":"item/started","params":{"threadId":"test-thread","turnId":"test-turn","item":{"id":"tool-1","type":"fileChange","changes":[{"path":"test.txt","diff":"+synthetic","kind":{"type":"add"}}]}}})
        key=self.approval(method="item/fileChange/requestApproval",command=None)
        self.assertTrue(self.c.pending[key]["reviewable"])
        self.c.answer("test-thread",{"key":key,"decision":"accept"})
    def test_inactive_and_secret_input_rejected(self):
        self.new()
        self.c.handle_event({"id":1,"method":"item/commandExecution/requestApproval","params":{"threadId":"test-thread","turnId":"not-active"}})
        self.assertEqual(self.c.pending,{})
        self.c.active["test-thread"]="test-turn"
        self.c.handle_event({"id":2,"method":"item/tool/requestUserInput","params":{"threadId":"test-thread","turnId":"test-turn","questions":[{"id":"q","isSecret":True}]}})
        self.assertEqual(self.c.pending,{})
    def test_history_filters_reasoning(self):
        self.new();self.c.history("test-thread")
        self.assertEqual([x["text"] for x in self.c.snapshot()["messages"]],["Saved artificial result"])
    def test_failed_new_turn_does_not_resend_old_reply(self):
        self.new();self.c.messages["test-thread"]=[{"role":"assistant","turn_id":"old-turn","text":"Old synthetic reply"}]
        self.c.active["test-thread"]="new-turn"
        self.c.handle_event({"method":"turn/completed","params":{"threadId":"test-thread","turn":{"id":"new-turn","status":"failed"}}})
        self.assertNotIn("Old synthetic reply",self.c.notifications.get_nowait())
    def test_disconnected_controller_does_not_dispatch(self):
        self.c.connected=False
        with self.assertRaises(SafeError):self.new()
        self.assertEqual(self.c.rpc.calls,[])
    def test_selected_progress_only_and_explicit_completion(self):
        directory=self.work/".task-progress";directory.mkdir()
        snapshot={"schema":"pbc-task-status/1","task_id":"test-thread","updated_at":datetime.now(timezone.utc).isoformat(),"finished_at":None,
                  "checkpoints":[{"label":"Synthetic stage","done":False,"percent":100}]}
        atomic_json(directory/"test-thread.json",snapshot)
        value=read_progress(self.work,"test-thread")
        self.assertEqual(value["percent"],100);self.assertFalse(value["complete"])
        self.assertFalse(read_progress(self.work,"other-thread")["available"])
        self.assertFalse(read_progress(self.work,"../test-thread")["available"])
        snapshot["task_id"]="other-thread";atomic_json(directory/"test-thread.json",snapshot)
        self.assertFalse(read_progress(self.work,"test-thread")["available"])
    def test_progress_canonical_workspace_alias_and_outside_target(self):
        directory=self.work/".task-progress";directory.mkdir()
        path=directory/"test-thread.json"
        snapshot={"schema":"pbc-task-status/1","task_id":"test-thread",
                  "updated_at":datetime.now(timezone.utc).isoformat(),"finished_at":None,
                  "checkpoints":[{"label":"Synthetic stage","done":False,"percent":25}]}
        atomic_json(path,snapshot)
        canonical=self.root/"canonical-work"
        resolved_file=canonical/".task-progress/test-thread.json"
        original_resolve=Path.resolve
        def resolve_alias(candidate,*args,**kwargs):
            # Model /var -> /private/var and Windows short-name aliases on any
            # test host, without requiring permission to create OS symlinks.
            if candidate==self.work:return canonical
            if candidate==path:return resolved_file
            return original_resolve(candidate,*args,**kwargs)
        with patch.object(Path,"resolve",autospec=True,side_effect=resolve_alias):
            value=read_progress(self.work,"test-thread")
            self.assertTrue(value["available"]);self.assertEqual(value["percent"],25)
            resolved_file=self.root/"outside/.task-progress/test-thread.json"
            self.assertFalse(read_progress(self.work,"test-thread")["available"])
    def test_progress_symlink_directory_and_file_still_rejected(self):
        directory=self.work/".task-progress";directory.mkdir()
        path=directory/"test-thread.json"
        atomic_json(path,{"schema":"pbc-task-status/1","task_id":"test-thread",
                          "updated_at":datetime.now(timezone.utc).isoformat(),"finished_at":None,
                          "checkpoints":[{"label":"Synthetic stage","done":False,"percent":25}]})
        for redirected in (directory,path):
            with self.subTest(redirected=redirected.name), patch.object(Path,"is_symlink",autospec=True,side_effect=lambda candidate:candidate==redirected):
                self.assertFalse(read_progress(self.work,"test-thread")["available"])
    def test_new_turn_rejects_fresh_previous_phase_completion(self):
        self.new()
        directory=self.work/".task-progress";directory.mkdir()
        now=datetime.now(timezone.utc)
        old={"schema":"pbc-task-status/1","task_id":"test-thread",
             "started_at":(now-timedelta(minutes=1)).isoformat(),"updated_at":now.isoformat(),
             "finished_at":now.isoformat(),"checkpoints":[{"label":"Previous phase","done":True,"percent":100}]}
        atomic_json(directory/"test-thread.json",old)
        self.assertTrue(self.c.snapshot()["progress"]["complete"])
        self.c.execute("request_new_phase","send",{"text":"New synthetic task"})
        self.assertFalse(self.c.snapshot()["progress"]["available"])
        current=dict(old,started_at=self.c.data["threads"]["test-thread"]["phase_started_at"],
                     updated_at=datetime.now(timezone.utc).isoformat(),finished_at=None,
                     checkpoints=[{"label":"Current phase","done":False,"percent":0}])
        atomic_json(directory/"test-thread.json",current)
        self.assertEqual(self.c.snapshot()["progress"]["percent"],0)
        self.assertFalse(self.c.snapshot()["progress"]["complete"])
    def test_progress_phase_is_saved_before_uncertain_dispatch_and_survives_restart(self):
        self.new()
        original=self.c.rpc.call
        def uncertain(method,params,**kwargs):
            if method=="turn/start":
                saved=json.loads((self.home/"state.json").read_text(encoding="utf-8"))
                self.assertIn("phase_started_at",saved["threads"]["test-thread"])
                raise SafeError("Synthetic uncertain dispatch")
            return original(method,params,**kwargs)
        with patch.object(self.c.rpc,"call",side_effect=uncertain):
            with self.assertRaises(SafeError):self.c.execute("request_phase_lost","send",{"text":"Synthetic task"})
        boundary=self.c.data["threads"]["test-thread"]["phase_started_at"]
        self.c.close();self.c=Controller(self.config,self.home,FakeRpc)
        self.assertEqual(self.c.data["threads"]["test-thread"]["phase_started_at"],boundary)
        with self.assertRaises(SafeError):self.c.execute("request_phase_lost","send",{"text":"Synthetic task"})
        self.assertEqual(self.c.data["threads"]["test-thread"]["phase_started_at"],boundary)
        directory=self.work/".task-progress";directory.mkdir()
        value={"schema":"pbc-task-status/1","task_id":"test-thread",
               "updated_at":datetime.now(timezone.utc).isoformat(),"finished_at":None,
               "checkpoints":[{"label":"Undated phase","done":True,"percent":100}]}
        atomic_json(directory/"test-thread.json",value)
        self.assertFalse(self.c.snapshot()["progress"]["available"])
    def test_steer_keeps_current_progress_phase(self):
        self.new();self.c.execute("request_phase_first","send",{"text":"Synthetic task"})
        boundary=self.c.data["threads"]["test-thread"]["phase_started_at"]
        self.c.active["test-thread"]="turn-current"
        self.c.execute("request_phase_steer","send",{"text":"Synthetic clarification"})
        self.assertEqual(self.c.rpc.calls[-1][0],"turn/steer")
        self.assertEqual(self.c.data["threads"]["test-thread"]["phase_started_at"],boundary)
        saved=json.loads((self.home/"state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["threads"]["test-thread"]["phase_started_at"],boundary)
    def test_attachment_and_export_confinement(self):
        for name in ("../secret.txt","folder/file.txt","script.exe"):
            with self.assertRaises(SafeError): self.c.upload({"name":name,"content":"aA=="})
        result=self.c.upload({"name":"artificial.txt","content":base64.b64encode(b"fixture").decode()})
        self.assertIn(result["file_id"],self.c.data["files"])
        root=self.work/"telegram-exports";root.mkdir();(root/"result.txt").write_text("synthetic")
        self.assertEqual(self.c.download("result.txt"),b"synthetic")
        with self.assertRaises(SafeError):self.c.download("../private/state.json")
    def test_instance_lock(self):
        lock=InstanceLock(self.home)
        try:
            with self.assertRaises(SafeError): InstanceLock(self.home)
        finally:lock.close()
    def test_bot_does_not_accept_another_user(self):
        bot=Telegram(TEST_TOKEN,OWNER,self.config["public_url"],self.c)
        with patch.object(bot,"send") as send:
            bot.handle({"update_id":1,"message":{"from":{"id":4343},"chat":{"id":4343,"type":"private"},"text":"/new test"}})
            send.assert_not_called()
        self.assertEqual(self.c.rpc.calls,[])


class HttpTests(unittest.TestCase):
    setUp = ControllerTests.setUp
    tearDown = ControllerTests.tearDown
    def test_http_auth_origin_and_no_arbitrary_rpc(self):
        bot=Telegram(TEST_TOKEN,OWNER,self.config["public_url"],self.c)
        server=Server(("127.0.0.1",0),handler_factory(self.c,bot,TEST_TOKEN,self.config))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            base="http://127.0.0.1:"+str(server.server_address[1])
            def request(path,auth=None,origin="https://panel.example.org"):
                headers={"Host":"panel.example.org","Content-Type":"application/json","Origin":origin}
                if auth:headers["X-Telegram-Init-Data"]=auth
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                return opener.open(urllib.request.Request(base+path,data=b"{}",headers=headers),timeout=3)
            with self.assertRaises(urllib.error.HTTPError) as denied:request("/api/state")
            self.assertEqual(denied.exception.code,401)
            with request("/api/state",signed()) as response:self.assertEqual(response.status,200)
            with self.assertRaises(urllib.error.HTTPError) as denied:request("/api/state",signed(),"https://evil.example")
            self.assertEqual(denied.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError) as denied:request("/api/rpc",signed())
            self.assertEqual(denied.exception.code,404)
        finally:
            server.shutdown();server.server_close();worker.join(3)


class CredentialTests(unittest.TestCase):
    def test_login_and_doctor_strip_parent_credentials_and_gateway(self):
        from codex_telegram.cli import login, doctor
        from types import SimpleNamespace
        from contextlib import redirect_stdout
        import io
        keys=("TELEGRAM_BOT_TOKEN","CODEX_CLI_PATH","CODEX_THREAD_ID","OPENAI_API_KEY",
              "CODEX_API_KEY","CODEX_APP_TOOLS_PIPE_PATH","CODEX_MCP_NODE_PATH")
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.dict(os.environ,{k:"synthetic-private-value" for k in keys}), \
                 patch("codex_telegram.cli.resolve_command",return_value=["synthetic-cli"]), \
                 patch("codex_telegram.cli.subprocess.run",return_value=SimpleNamespace(returncode=0)) as run, \
                 patch("codex_telegram.cli.platform.system",return_value="SyntheticOS"), \
                 patch("codex_telegram.cli.platform.machine",return_value="SyntheticCPU"), \
                 patch("codex_telegram.cli.token",return_value=TEST_TOKEN), redirect_stdout(io.StringIO()):
                config={"codex_command":["synthetic-cli"],"workspace":str(root)}
                login(config,root)
                self.assertEqual(doctor(config,root),0)
                self.assertEqual(run.call_count,2)
                for call in run.call_args_list:
                    environment=call.kwargs["env"]
                    self.assertFalse(set(keys)&set(environment))
                    self.assertEqual(environment["CODEX_HOME"],str(root/"codex"))
    def test_token_recovery_stores_only_in_keyring_and_releases_lock(self):
        from codex_telegram.cli import set_token
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);secret="123456:"+"x"*35;output=io.StringIO()
            with patch("codex_telegram.cli.load",return_value={}), \
                 patch("codex_telegram.cli.getpass.getpass",return_value=secret), \
                 patch("codex_telegram.cli.secure_keyring") as keyring, redirect_stdout(output):
                set_token(root)
                keyring.return_value.set_password.assert_called_once_with("codex-telegram-control",str(root.resolve()),secret)
            self.assertNotIn(secret,output.getvalue())
            self.assertFalse((root/"config.json").exists())
            lock=InstanceLock(root);lock.close()


class ProtocolTests(unittest.TestCase):
    def test_real_subprocess_handshake_and_turn_without_network(self):
        with tempfile.TemporaryDirectory(prefix="ctc protocol ") as temp:
            root=Path(temp);work=root/"work";work.mkdir();home=root/"home";home.mkdir()
            config=validate({"schema":1,"owner_id":OWNER,"workspace":str(work),"public_url":"https://panel.example.org"})
            command=[sys.executable,"-B",str(Path(__file__).with_name("fake_app_server.py"))]
            c=Controller(config,home,lambda callback:Rpc(command,str(work),home/"codex",callback))
            try:
                c.execute("protocol_new_1","new",{"title":"Protocol fixture"})
                c.execute("protocol_send_1","send",{"text":"Artificial prompt"})
                deadline=time.monotonic()+5
                while c.statuses.get("fixture-thread")!="completed" and time.monotonic()<deadline:time.sleep(.02)
                self.assertEqual(c.statuses.get("fixture-thread"),"completed")
                self.assertTrue(any(x["text"]=="Artificial reply" for x in c.snapshot()["messages"]))
                self.assertEqual(c.models()[0]["model"],"fixture-model")
            finally:c.close()


if __name__ == "__main__":
    unittest.main()

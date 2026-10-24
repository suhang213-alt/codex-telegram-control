"""Desktop gateway protocol fixtures. No live Desktop, model, or network."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from codex_telegram.config import SafeError
from codex_telegram.desktop_gateway import GatewayCore, is_engine_invocation, wire, write_all, MAX_FRAME


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="desktop-gateway-test-")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.work=self.root/"workspace";self.work.mkdir()
        self.outside=self.root/"outside";self.outside.mkdir()
        self.engine=[];self.gui=[];self.journal=[];self.drop=None
        self.responses={}
        self.core=GatewayCore({"workspace":str(self.work)},self.engine_write,self.gui.append,
                              persist=lambda value:self.journal.append(copy.deepcopy(value)),rpc_timeout=.5,epoch="synthetic-epoch")
        self.addCleanup(self.core.close)
        self.network=patch('socket.socket',side_effect=AssertionError('No network allowed'))
        self.child=patch('subprocess.Popen',side_effect=AssertionError('No processes allowed'))
        self.network_mock=self.network.start();self.child_mock=self.child.start()
        self.addCleanup(self.network.stop);self.addCleanup(self.child.stop)
        self.addCleanup(self.network_mock.assert_not_called);self.addCleanup(self.child_mock.assert_not_called)

    def engine_write(self,line):
        self.engine.append(line)
        try: request=json.loads(line)
        except ValueError:return
        identifier=request.get("id")
        if not isinstance(identifier,str) or not identifier.startswith("ctd-"):return
        method=request.get("method");params=request.get("params",{})
        if method==self.drop:return
        if method in self.responses: result=self.responses[method]
        elif method=="thread/read":result={"thread":{"id":params["threadId"],"cwd":str(self.outside if params["threadId"]=="outside" else self.work),"turns":[]}}
        elif method=="thread/list":result={"data":[{"id":"inside","cwd":str(self.work)},{"id":"outside","cwd":str(self.outside)}],"nextCursor":None}
        elif method=="thread/resume":result={"thread":{"id":params["threadId"],"cwd":str(self.work)}}
        elif method=="turn/start":result={"turn":{"id":"active-turn","status":"inProgress"}}
        else:result={}
        self.core.from_engine(wire({"id":identifier,"result":result}))

    def ready(self):
        self.core.from_gui(wire({"id":1,"method":"initialize","params":{}}))
        self.core.from_engine(wire({"id":1,"result":{"serverInfo":{"name":"fixture"}}}))
        self.core.from_gui(wire({"method":"initialized"}))

    def call(self,method,params,operation="operation-0001"):
        return self.core.command({"epoch":self.core.epoch,"command":"rpc","method":method,"params":params,"operation_id":operation})

    def pending(self,method="item/commandExecution/requestApproval",request_id=7,**extra):
        self.ready();self.call("thread/list",{})
        self.core.from_engine(wire({"method":"turn/started","params":{"threadId":"inside","turn":{"id":"active-turn"}}}))
        request={"id":request_id,"method":method,"params":{"threadId":"inside","turnId":"active-turn","itemId":"item1","command":"echo fixture",**extra}}
        self.core.from_engine(wire(request))
        return request

    def reply(self,request,decision="accept",operation="reply-operation1"):
        return self.core.command({"epoch":self.core.epoch,"command":"reply","request_id":request["id"],"thread_id":"inside",
                                  "turn_id":"active-turn","result":{"decision":decision},"operation_id":operation})

    def test_no_independent_initialize_and_not_ready_gate(self):
        with self.assertRaises(SafeError):self.call("thread/list",{})
        self.assertEqual(self.engine,[])
        self.core.from_gui(wire({"id":1,"method":"initialize","params":{}}))
        self.core.from_gui(wire({"method":"initialized"}))
        self.assertFalse(self.core.status()["ready"])
        self.core.from_engine(wire({"id":1,"result":{}}))
        self.assertTrue(self.core.status()["ready"])
        self.assertEqual(sum(json.loads(x).get("method")=="initialize" for x in self.engine),1)

    def test_desktop_initialize_ack_without_notification_is_ready(self):
        changes=[];self.core.on_status=lambda:changes.append(self.core.ready)
        request=wire({"id":"desktop-init","method":"initialize","params":{}})
        self.core.from_gui(request)
        self.assertEqual(self.engine,[request])
        self.assertFalse(self.core.ready)
        self.core.from_engine(wire({"id":"desktop-init","result":{}}))
        self.assertTrue(self.core.ready)
        self.assertFalse(self.core.status()["handshake"]["initialized"])
        self.assertEqual(changes,[True])
        self.assertEqual(self.call("thread/list",{})["data"][0]["id"],"inside")
        self.assertEqual(sum(json.loads(frame).get("method")=="initialize" for frame in self.engine),1)
        self.core.compatibility_check=lambda:False
        self.assertFalse(self.core.ready)
        self.core.compatibility_check=lambda:True
        self.core.connected=False
        self.assertFalse(self.core.ready)

    def test_unrelated_or_failed_initialize_reply_does_not_unlock_gateway(self):
        self.core.from_engine(wire({"id":42,"result":{}}))
        self.assertFalse(self.core.ready)
        self.assertFalse(self.core.status()["handshake"]["initialize_requested"])
        self.core.from_gui(wire({"id":42,"method":"initialize","params":{}}))
        for identifier in ("42",43):
            self.core.from_engine(wire({"id":identifier,"result":{}}))
            self.assertFalse(self.core.ready)
        self.core.from_engine(wire({"id":42,"error":{"message":"synthetic failure"}}))
        self.core.from_gui(wire({"method":"initialized"}))
        self.assertFalse(self.core.ready)
        before=list(self.engine)
        with self.assertRaises(SafeError):self.call("thread/list",{})
        self.assertEqual(self.engine,before)

    def test_malformed_initialize_response_cannot_unlock_gateway(self):
        for index, body in enumerate(({}, {"result":None}, {"result":[]}, {"result":"invalid"}, {"result":1})):
            with self.subTest(body=body):
                identifier="init-"+str(index)
                self.core.from_gui(wire({"id":identifier,"method":"initialize","params":{}}))
                self.core.from_engine(wire(dict(body,id=identifier)))
                self.assertFalse(self.core.ready)
                self.assertFalse(self.core.status()["handshake"]["initialize_ok"])
                with self.assertRaises(SafeError):self.call("thread/list",{})
        self.core.from_gui(wire({"id":"valid-init","method":"initialize","params":{}}))
        self.core.from_engine(wire({"id":"valid-init","result":{}}))
        self.assertTrue(self.core.ready)

    def test_unknown_and_malformed_frames_pass_exactly(self):
        frames=[b'{"method":"future/notice","params":{"thread":"unexpected"}}\n', b'not json\n', b'{"method":[],"params":null}\n']
        for frame in frames:self.core.from_gui(frame);self.core.from_engine(frame)
        self.assertEqual(self.engine,frames);self.assertEqual(self.gui,frames)

    def test_handshake_diagnostics_observe_success_without_payloads(self):
        expected={"initialize_requested":False,"initialize_failed":False,"initialize_ok":False,"initialized":False}
        self.assertEqual(self.core.status()["handshake"],expected)
        self.core.from_gui(wire({"id":"PRIVATE_INIT_ID","method":"initialize","params":{"private":"PRIVATE_INIT_PARAMS"}}))
        expected["initialize_requested"]=True
        self.assertEqual(self.core.status()["handshake"],expected)
        self.assertFalse(self.core.ready)
        self.core.from_engine(wire({"id":"PRIVATE_INIT_ID","result":{"private":"PRIVATE_INIT_RESULT"}}))
        expected["initialize_ok"]=True
        self.assertEqual(self.core.status()["handshake"],expected)
        self.assertTrue(self.core.ready)
        self.core.from_gui(wire({"method":"initialized"}))
        expected["initialized"]=True
        status=self.core.status()
        self.assertEqual(status["handshake"],expected)
        self.assertTrue(status["ready"])
        self.assertTrue(all(type(value) is bool for value in status["handshake"].values()))
        self.assertNotIn("PRIVATE_INIT",json.dumps(status))

    def test_handshake_failure_requires_matching_response_and_keeps_ready_gate(self):
        self.core.from_gui(wire({"id":42,"method":"initialize","params":{}}))
        self.core.from_engine(wire({"id":"42","error":{"message":"PRIVATE_UNRELATED_ERROR"}}))
        self.assertFalse(self.core.status()["handshake"]["initialize_failed"])
        self.core.from_engine(wire({"id":42,"error":{"message":"PRIVATE_INIT_ERROR"}}))
        self.core.from_gui(wire({"method":"initialized"}))
        self.assertEqual(self.core.status()["handshake"],{
            "initialize_requested":True,"initialize_failed":True,"initialize_ok":False,"initialized":True})
        self.assertFalse(self.core.ready)
        # A successful Desktop retry retains the failure observation, while
        # readiness continues to use only its original conditions.
        self.core.from_gui(wire({"id":43,"method":"initialize","params":{}}))
        self.core.from_engine(wire({"id":43,"result":{}}))
        self.assertTrue(self.core.ready)
        self.assertTrue(self.core.status()["handshake"]["initialize_failed"])
        self.assertNotIn("PRIVATE_",json.dumps(self.core.status()))

    def test_scope_filters_list_and_rejects_foreign_read_mutation(self):
        self.ready()
        self.assertEqual([t["id"] for t in self.call("thread/list",{})["data"]],["inside"])
        for method,params in [("thread/read",{"threadId":"outside","includeTurns":True}),("turn/start",{"threadId":"outside","input":[{"type":"text","text":"fixture"}]})]:
            with self.assertRaises(SafeError):self.call(method,params)
        outside=[json.loads(x) for x in self.engine if json.loads(x).get("params",{}).get("threadId")=="outside"]
        self.assertTrue(all(x["method"]=="thread/read" and x["params"]["includeTurns"] is False for x in outside))

    def test_desktop_thread_source_filter_is_forwarded_only_for_thread_list(self):
        self.ready()
        params={"sourceKinds":["cli","vscode","appServer"]}
        result=self.call("thread/list",params)
        self.assertEqual([row["id"] for row in result["data"]],["inside"])
        self.assertEqual(json.loads(self.engine[-1])["params"],params)
        for invalid in ("appServer",["unknown"],[{}],["cli"]*4):
            with self.assertRaises(SafeError):self.call("thread/list",{"sourceKinds":invalid})
        with self.assertRaises(SafeError):self.call("model/list",params)

    def test_scope_is_refreshed_before_mutation(self):
        self.ready();self.call("thread/list",{})
        self.responses["thread/read"]={"thread":{"id":"inside","cwd":str(self.outside)}}
        with self.assertRaises(SafeError):self.call("turn/start",{"threadId":"inside","input":[{"type":"text","text":"fixture"}]})
        self.assertFalse(any(json.loads(x).get("method")=="turn/start" for x in self.engine))

    def test_override_and_arbitrary_rpc_rejected(self):
        self.ready()
        for method,params in [("thread/start",{}),("thread/resume",{"threadId":"inside","sandbox":"danger-full-access"}),("turn/start",{"threadId":"inside","input":[{"type":"text","text":"fixture"}],"cwd":str(self.outside)})]:
            with self.assertRaises(SafeError):self.call(method,params)

    def test_mutation_is_durable_before_write_and_deduplicated_without_prompts(self):
        self.ready()
        result=self.call("turn/start",{"threadId":"inside","input":[{"type":"text","text":"PRIVATE_SYNTHETIC_PROMPT"}]})
        self.assertEqual(result["turn"]["id"],"active-turn")
        self.assertTrue(any(row["operations"].get("operation-0001",{}).get("state")=="dispatching" for row in self.journal))
        self.call("turn/start",{"threadId":"inside","input":[{"type":"text","text":"PRIVATE_SYNTHETIC_PROMPT"}]})
        self.assertEqual(sum(json.loads(x).get("method")=="turn/start" for x in self.engine),1)
        self.assertNotIn("PRIVATE_SYNTHETIC_PROMPT",json.dumps(self.journal))

    def test_unknown_is_never_replayed(self):
        self.ready();self.drop="turn/start"
        payload={"threadId":"inside","input":[{"type":"text","text":"fixture"}]}
        with self.assertRaises(SafeError):self.call("turn/start",payload)
        with self.assertRaises(SafeError):self.call("turn/start",payload)
        self.assertEqual(sum(json.loads(x).get("method")=="turn/start" for x in self.engine),1)

    def test_stale_epoch_and_changed_payload_rejected(self):
        self.ready()
        with self.assertRaises(SafeError):self.core.command({"epoch":"old","command":"status"})
        self.call("turn/start",{"threadId":"inside","input":[{"type":"text","text":"first"}]})
        with self.assertRaises(SafeError):self.call("turn/start",{"threadId":"inside","input":[{"type":"text","text":"changed"}]})

    def test_typed_id_and_active_turn_are_required(self):
        request=self.pending()
        with self.assertRaises(SafeError):self.reply(dict(request,id="7"))
        self.core.active["inside"]="different-turn"
        with self.assertRaises(SafeError):self.reply(request,operation="reply-operation2")
        self.assertFalse(any("result" in json.loads(x) for x in self.engine))

    def test_desktop_winner_prevents_remote_reply_and_does_not_forge_resolved(self):
        request=self.pending()
        self.core.from_gui(wire({"id":request["id"],"result":{"decision":"decline"}}))
        with self.assertRaises(SafeError):self.reply(request)
        self.assertEqual(self.core.status()["pending"],[])
        events=self.core.command({"epoch":self.core.epoch,"command":"events","after":0})["events"]
        self.assertTrue(any(x["event"]["method"]=="gateway/requestClaimed" for x in events))
        self.assertFalse(any(x["event"]["method"]=="serverRequest/resolved" for x in events))

    def test_remote_winner_suppresses_duplicate_gui_but_real_resolution_passes(self):
        request=self.pending();self.reply(request)
        before=len(self.engine)
        self.core.from_gui(wire({"id":request["id"],"result":{"decision":"accept"}}))
        self.assertEqual(len(self.engine),before)
        resolved=wire({"method":"serverRequest/resolved","params":{"requestId":7,"threadId":"inside"}})
        self.core.from_engine(resolved)
        self.assertEqual(self.gui[-1],resolved)

    def test_secret_request_only_reaches_gui(self):
        request=self.pending("item/tool/requestUserInput",questions=[{"id":"q","question":"Password","isSecret":True}])
        self.assertEqual(self.core.status()["pending"],[])
        self.assertIn(wire(request),self.gui)

    def test_full_file_context_required_for_accept(self):
        request=self.pending("item/fileChange/requestApproval",command=None)
        with self.assertRaises(SafeError):self.reply(request)
        self.reply(request,"decline",operation="reply-operation2")

    def test_interrupt_requires_exact_active_turn(self):
        self.pending()
        with self.assertRaises(SafeError):self.call("turn/interrupt",{"threadId":"inside","turnId":"stale"})
        self.call("turn/interrupt",{"threadId":"inside","turnId":"active-turn"},operation="stop-operation2")
        self.assertEqual(sum(json.loads(x).get("method")=="turn/interrupt" for x in self.engine),1)

    def test_event_overflow_requires_reconciliation(self):
        self.ready();self.call("thread/list",{})
        for i in range(1002):self.core.from_engine(wire({"method":"item/agentMessage/delta","params":{"threadId":"inside","delta":"fixture"}}))
        result=self.core.command({"epoch":self.core.epoch,"command":"events","after":0})
        self.assertTrue(result["gap"]);self.assertEqual(len(result["events"]),200)
        self.assertLess(result["cursor"],self.core.cursor)
        following=self.core.command({"epoch":self.core.epoch,"command":"events","after":result["cursor"]})
        self.assertFalse(following["gap"])
        self.assertEqual(following["events"][0]["seq"],result["cursor"]+1)

    def test_engine_invocation_detection_preserves_cli_subcommands(self):
        self.assertTrue(is_engine_invocation(["-c","features.code_mode_host=true","app-server","--analytics-default-enabled"]))
        self.assertFalse(is_engine_invocation(["app-server","generate-json-schema"]))
        self.assertFalse(is_engine_invocation(["--version"]))
        self.assertFalse(is_engine_invocation(["login"]))

    def test_multiple_engines_disable_broker_without_changing_gui_transport(self):
        self.ready()
        self.core.compatibility_check=lambda:False
        status=self.core.status()
        self.assertTrue(status["ambiguous"]);self.assertFalse(status["ready"])
        events=self.core.command({"epoch":self.core.epoch,"command":"events","after":0})
        self.assertTrue(events["ambiguous"]);self.assertFalse(events["ready"])
        before=len(self.engine)
        with self.assertRaises(SafeError):self.call("thread/list",{})
        self.assertEqual(len(self.engine),before)
        frame=wire({"id":999,"method":"future/gui-operation","params":{}})
        self.core.from_gui(frame)
        self.assertEqual(self.engine[-1],frame)

    def test_readiness_callback_fires_once_after_initialize_ack(self):
        changes=[];self.core.on_status=lambda:changes.append(self.core.ready)
        self.ready()
        self.assertEqual(changes,[True])

    def test_oversized_gui_frame_cannot_be_interleaved_with_broker_rpc(self):
        self.ready()
        prefix=b'{"id":999,"method":"future/large","params":{"text":"'+b'x'*MAX_FRAME
        tail=b'"}}\n'
        before=len(self.engine)
        self.core.from_gui(prefix)
        with self.assertRaises(SafeError):self.call("model/list",{})
        self.assertEqual(len(self.engine),before+1)
        self.core.from_gui(tail)
        self.assertEqual(b''.join(self.engine[before:]),prefix+tail)
        self.call("model/list",{})
        self.assertEqual(json.loads(self.engine[-1])["method"],"model/list")

    def test_partial_raw_writes_are_completed_before_flush(self):
        class PartialStream:
            def __init__(self):self.body=bytearray();self.flushes=0
            def write(self,data):
                amount=min(3,len(data));self.body.extend(data[:amount]);return amount
            def flush(self):self.flushes+=1
        stream=PartialStream();payload=b'{"synthetic":"frame"}\n'
        write_all(stream,payload)
        self.assertEqual(bytes(stream.body),payload);self.assertEqual(stream.flushes,1)
        with patch.object(stream,"write",return_value=0):
            with self.assertRaises(OSError):write_all(stream,payload)

    def test_oversized_broker_response_with_id_last_never_reaches_gui(self):
        self.ready(); before=list(self.gui)
        def respond(line):
            self.engine.append(line)
            request=json.loads(line)
            # A nested method is payload, and escaped root key spelling is valid.
            payload=(b'{"result":{"method":"nested","text":"'+b'x'*MAX_FRAME+
                     b'"},"i\\u0064":'+json.dumps(request["id"]).encode()+b'}\n')
            self.core.from_engine(payload[:MAX_FRAME+1])
            self.assertEqual(self.gui,before)
            self.core.from_engine(payload[MAX_FRAME+1:])
        self.core.write_engine=respond
        with self.assertRaisesRegex(SafeError,"8 MiB broker limit"):self.call("model/list",{})
        self.assertEqual(self.gui,before)
        self.assertIsNone(self.core.engine_spool)

    def test_oversized_desktop_frame_ignores_nested_ids_and_replays_exact_bytes(self):
        self.ready(); before=len(self.gui)
        payload=wire({"result":{"id":"ctd-"+self.core.epoch+"-55",
                                "text":('quoted \\" brackets } [ key id '+"x"*MAX_FRAME)},"id":999})
        self.core.from_engine(payload[:MAX_FRAME+1])
        self.assertEqual(len(self.gui),before)
        self.core.from_engine(payload[MAX_FRAME+1:])
        self.assertEqual(b''.join(self.gui[before:]),payload)
        self.assertIsNone(self.core.engine_spool)

    def test_incomplete_engine_spool_is_closed_without_reinterpreting_tail(self):
        self.core.from_engine(b'{"result":{"text":"'+b'x'*MAX_FRAME)
        spool=self.core.engine_spool
        self.assertIsNotNone(spool); self.assertEqual(self.gui,[])
        self.core.close()
        self.assertTrue(spool.closed); self.assertIsNone(self.core.engine_spool)


if __name__=="__main__":unittest.main()

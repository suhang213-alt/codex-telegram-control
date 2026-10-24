"""Synthetic portability, owner identity and workspace boundary regressions."""
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import bridge_config as config
import telegram_bridge as bridge
import miniapp_server as api
import work_scope

THREAD="11111111-1111-4111-8111-111111111111"
SERVICE="22222222-2222-4222-8222-222222222222"
OWNER=123456


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.workspace=self.root/"workspace"
        self.workspace.mkdir()
        self.home=self.root/"codex"
        self.home.mkdir()
        self.value={"schema":"codex-telegram-desktop/1","owner_user_id":OWNER,"service_thread_id":SERVICE,
                    "allowed_workspaces":[str(self.workspace)],"codex_home":str(self.home),
                    "compatibility_acknowledged":True,"tunnel_provider":"none"}
        self.mock=patch.object(config,"settings",return_value=self.value)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_owner_is_numeric_and_username_independent(self):
        self.assertTrue(config.owner_matches({"id":OWNER,"username":"changed_handle"}))
        self.assertFalse(config.owner_matches({"id":OWNER+1,"username":"example_owner"}))
        self.assertFalse(config.owner_matches({"id":str(OWNER)}))
        self.assertFalse(config.owner_matches({"id":True}))

    def test_allowlist_rejects_sibling_and_prefix_collision(self):
        sibling=self.root/"workspace-other"
        sibling.mkdir()
        self.assertTrue(config.allowed_workspace(self.workspace))
        self.assertFalse(config.allowed_workspace(sibling))
        self.assertFalse(config.allowed_workspace(self.root))

    def test_scope_uses_verified_metadata_not_request_path(self):
        with closing(sqlite3.connect(self.home/"state_1.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT,cwd TEXT)")
            db.execute("INSERT INTO threads VALUES(?,?)",(THREAD,str(self.workspace)))
            db.execute("INSERT INTO threads VALUES(?,?)",(SERVICE,str(self.workspace)))
            db.commit()
        self.assertTrue(work_scope.allowed({"id":THREAD,"kind":"codex","cwd":"untrusted"},set()))
        self.assertFalse(work_scope.allowed({"id":SERVICE,"kind":"codex"},set()))
        self.assertFalse(work_scope.allowed({"id":THREAD,"kind":"chatgpt"},{THREAD}))
        with closing(sqlite3.connect(self.home/"state_1.sqlite")) as db:
            db.execute("UPDATE threads SET cwd=? WHERE id=?",(str(self.root),THREAD))
            db.commit()
        self.assertFalse(config.thread_allowed(THREAD))

    def test_missing_or_ambiguous_plugin_fails_closed(self):
        root=self.home/"plugins/cache/openai-bundled/codex-app-tools"
        self.assertFalse((config.plugin_dir()/"server.mjs").exists())
        (root/"1").mkdir(parents=True)
        (root/"1/server.mjs").write_text("// artificial fixture")
        self.assertEqual(config.plugin_dir(),(root/"1").resolve())
        (root/"2").mkdir()
        (root/"2/server.mjs").write_text("// artificial fixture")
        self.assertFalse((config.plugin_dir()/"server.mjs").exists())

    def test_config_without_explicit_compatibility_ack_is_rejected(self):
        value=dict(self.value,compatibility_acknowledged=False)
        with self.assertRaises(ValueError):config.validate(value)

    def test_initdata_identity_signature_and_freshness(self):
        token="artificial-test-key"
        now=2000000000
        def signed(owner=OWNER,at=now):
            fields={"auth_date":str(at),"user":json.dumps({"id":owner,"username":"changed_handle"},separators=(",",":"))}
            secret=hmac.new(b"WebAppData",token.encode(),hashlib.sha256).digest()
            fields["hash"]=hmac.new(secret,"\n".join(f"{k}={v}" for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
            return urlencode(fields)
        with patch.object(bridge,"state",return_value={"owner_user_id":OWNER}),patch.object(bridge,"token",return_value=token):
            self.assertTrue(api.verify_init_data(signed(),now=now))
            self.assertFalse(api.verify_init_data(signed(owner=OWNER+1),now=now))
            self.assertFalse(api.verify_init_data(signed(at=now-3601),now=now))
            self.assertFalse(api.verify_init_data(signed()+"&auth_date="+str(now),now=now))
            self.assertFalse(api.verify_init_data(signed().replace("hash=","hash=f"),now=now))

    def test_pairing_rejects_foreign_numeric_owner_without_side_effects(self):
        state={"pair_code":"artificial-pair-code","owner_user_id":None}
        update={"update_id":1,"message":{"text":"/start artificial-pair-code",
                "from":{"id":OWNER+1,"username":"example_owner"},"chat":{"id":OWNER+1,"type":"private"}}}
        with patch.object(bridge,"state",return_value=state),patch.object(bridge,"save_state") as saved,patch.object(bridge,"api") as network:
            bridge.process_update(update)
            saved.assert_not_called()
            network.assert_not_called()


if __name__=="__main__":unittest.main()

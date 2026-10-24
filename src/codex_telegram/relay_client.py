"""Host-side client for the fixed, loopback-published Docker control port."""
from __future__ import annotations
import json
import os
import secrets
import threading
import urllib.request
from .config import SafeError
from .relay_protocol import (CONTROL_URL, MAX_WIRE, Verifier, binding, json_bytes, pack_body,
                             public_origin, signed, unpack_body)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise SafeError("Relay redirects are disabled.")


def enabled():
    return "CTD_RELAY_KEY_FILE" in os.environ


class RelayClient:
    def __init__(self, key, token, owner, public_url, platform, *, exchange=None):
        self.key = key
        self.verify = Verifier(key)
        self._exchange = exchange or self._http
        self.host_id = secrets.token_hex(16)
        self.epoch = self.lease = None
        self.public_url = public_origin(public_url)
        self.owner = owner
        self.stopped = threading.Event()
        self.backend_ready = False
        self.lock = threading.RLock()
        self.join_payload = {"host_id": self.host_id, "owner_id": owner, "public_url": self.public_url,
                             "bot_binding": binding(token), "platform": platform}

    def _http(self, path, request):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        raw = json_bytes(request)
        if len(raw) > MAX_WIRE: raise SafeError("Relay request exceeds the limit.")
        try:
            with opener.open(urllib.request.Request(CONTROL_URL + path, data=raw,
                             headers={"Content-Type": "application/json"}), timeout=38) as response:
                body = response.read(MAX_WIRE + 1)
            if len(body) > MAX_WIRE: raise ValueError()
            return json.loads(body)
        except (OSError, ValueError):
            raise SafeError("Docker relay response is unconfirmed. No automatic resend; check Desktop or Telegram.") from None

    def request(self, action, payload):
        if self.stopped.is_set(): raise SafeError("Docker connector stopped; restart it to reconnect.")
        path = "/" + action
        with self.lock:
            value = dict(payload)
            if action not in {"hello", "join"}: value.update(epoch=self.epoch, lease=self.lease)
        request = signed(self.key, "request:" + path, value)
        response = self.verify.verify("response:" + path, self._exchange(path, request), expected_nonce=request["nonce"])
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise SafeError("Docker relay rejected the operation; the outcome may be uncertain. Reconnect and review before retrying.")
        return response["result"]

    def join(self):
        hello = self.request("hello", {})
        if not isinstance(hello, dict) or not isinstance(hello.get("epoch"), str):
            raise SafeError("Invalid relay challenge.")
        result = self.request("join", dict(self.join_payload, epoch=hello["epoch"]))
        if not isinstance(result, dict) or not all(isinstance(result.get(k), str) for k in ("epoch", "lease")):
            raise SafeError("Invalid relay handshake.")
        with self.lock: self.epoch, self.lease = result["epoch"], result["lease"]

    def telegram(self, method, payload, *, document=None, file_path=None):
        result = self.request("telegram", {"method": method, "payload": payload or {}, "document": document,
                               "file_path": file_path, "operation_id": secrets.token_hex(16)})
        return result["result"]

    def document(self, name, content):
        return self.telegram("sendDocument", {"chat_id": self.owner}, document={"name": name, "body": pack_body(content)})

    def download(self, file_path):
        return unpack_body(self.telegram("getFile", {}, file_path=file_path)["body"])


_client = None
_connector = None
_lock = threading.RLock()


def start(token, owner, public_url, platform):
    global _client, _connector
    if not enabled(): return None
    from .host_connector import HostConnector
    from .host_secrets import read_host_key
    with _lock:
        if _client is not None: raise SafeError("Docker host connector is already running in this process.")
        configured_url = public_origin(os.environ.get("CTD_RELAY_PUBLIC_URL", ""))
        if configured_url != public_origin(public_url): raise SafeError("Host and Docker panel URLs differ.")
        client = RelayClient(read_host_key(os.environ["CTD_RELAY_KEY_FILE"]), token, owner, configured_url, platform)
        client.join()
        connector = HostConnector(client)
        _client, _connector = client, connector
        # Native entrypoints activate workers only after binding their own
        # localhost listener. A pre-existing listener must never receive jobs.
        return connector


def current():
    with _lock:
        if _client is None or _client.stopped.is_set(): raise SafeError("Docker host connector is not ready. Direct Telegram fallback is disabled.")
        return _client


def close():
    global _client, _connector
    with _lock:
        if _connector: _connector.stop()
        _client = _connector = None

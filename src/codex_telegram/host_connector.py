"""Reverse HTTP worker to an existing local adapter, never to a caller URL."""
from __future__ import annotations
import http.client
import threading
from urllib.parse import urlsplit
from .config import SafeError
from .relay_protocol import MAX_BODY, filter_headers, pack_body, unpack_body, validate_route


def local_request(method, path, headers, body, public_url, *, _connection_factory=None):
    validate_route(method, path)
    headers = filter_headers(headers)
    headers["Host"] = urlsplit(public_url).netloc
    headers["Connection"] = "close"
    headers["Content-Length"] = str(len(body))
    # http.client has no environment proxy or redirect support. The destination
    # is fixed and never derived from a request field, Location, or URL header.
    connection = (_connection_factory or http.client.HTTPConnection)("127.0.0.1", 8787, timeout=25)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.read(MAX_BODY + 1)
        if len(result) > MAX_BODY: raise SafeError("Host response exceeds the 21 MB Docker transport limit.")
        entity_length = len(result)
        if method == "HEAD":
            entity_length = int(response.getheader("Content-Length", "0"))
            if not 0 <= entity_length <= MAX_BODY: raise SafeError("Host response exceeds the 21 MB Docker transport limit.")
        return {"status": response.status, "headers": filter_headers(dict(response.getheaders()), response=True),
                "body": pack_body(result), "entity_length": entity_length}
    finally:
        connection.close()


class HostConnector:
    def __init__(self, client, *, forward=local_request, ready=None):
        self.client, self.forward = client, forward
        self.ready = ready or self._ready
        self.workers = []

    def _ready(self):
        # Only transport liveness; this is not a claim about model login or a turn.
        try:
            result = self.forward("GET", "/", {}, b"", self.client.public_url)
            return result["status"] == 200
        except (OSError, SafeError, http.client.HTTPException): return False

    def once(self):
        self.client.backend_ready = bool(self.ready())
        result = self.client.request("poll", {"backend_ready": self.client.backend_ready})
        job = result.get("job")
        if not job: return
        try:
            response = self.forward(job["method"], job["path"], job["headers"], unpack_body(job["body"]), self.client.public_url)
        except Exception:
            # Never retry: the local handler may have executed a durable mutation.
            response = {"status": 503, "headers": {"content-type": "application/json"},
                        "body": pack_body(b'{"error":"Local response unconfirmed; check Desktop before retrying"}')}
        self.client.request("complete", {"id": job["id"], "response": response})

    def _run(self):
        while not self.client.stopped.is_set():
            try:
                self.once()
                if self.client.stopped.wait(.2): break
            except Exception:
                # A changed epoch, expired lease, or lost completion requires a
                # deliberate reconnect; this worker never replays a mutation.
                self.client.stopped.set()
                self.client.backend_ready = False

    def start(self):
        for _ in range(4):
            worker = threading.Thread(target=self._run, daemon=True, name="DesktopDockerConnector")
            self.workers.append(worker); worker.start()

    def stop(self):
        self.client.stopped.set()
        self.client.backend_ready = False

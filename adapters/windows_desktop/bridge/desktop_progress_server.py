"""Read-only loopback task progress window. No models, enqueue or auth-file access.

This local owner session UI has no public tunnel. Telegram uses its separate
authenticated Mini App routes and the exact same progress provider.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
from urllib.parse import parse_qsl, urlsplit
import miniapp_server as api

HOST, PORT = '127.0.0.1', 55336
STATIC = Path(__file__).resolve().with_name('miniapp')
FILES = {'/': 'desktop-progress.html', '/index.html': 'desktop-progress.html',
         '/desktop-progress.js': 'desktop-progress.js', '/task-progress.js': 'task-progress.js',
         '/task-progress.css': 'task-progress.css'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status, payload, content_type='application/json; charset=utf-8'):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def static(self, path):
        relative = FILES.get(path)
        if relative is None:
            return self.respond(404, {'available': False})
        try:
            body = (STATIC / relative).read_bytes()
        except OSError:
            return self.respond(503, {'available': False, 'reason': 'asset_unavailable'})
        kind = 'text/html' if relative.endswith('.html') else 'text/css' if relative.endswith('.css') else 'text/javascript'
        return self.respond(200, body, kind + '; charset=utf-8')

    def do_GET(self):
        expected = 'http://' + HOST + ':' + str(PORT)
        if self.headers.get('Host') != HOST + ':' + str(PORT):
            return self.respond(403, {'available': False})
        if self.headers.get('Origin') not in (None, expected):
            return self.respond(403, {'available': False})
        referer = self.headers.get('Referer')
        if referer and not referer.startswith(expected + '/'):
            return self.respond(403, {'available': False})
        parsed = urlsplit(self.path)
        if parsed.path == '/_progress_health':
            return self.respond(200, {'ok': True, 'service': 'local-task-progress', 'read_only': True})
        selected = re.fullmatch(r'/api/threads/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/progress', parsed.path)
        if selected:
            identifier = selected.group(1)
            try:
                api.bridge.require_thread_scope(identifier)
            except ValueError:
                return self.respond(404, {'available': False})
            except Exception:
                return self.respond(503, {'available': False, 'reason': 'scope_unavailable'})
            try:
                return self.respond(200, api.task_progress_snapshot(identifier))
            except Exception:
                return self.respond(503, {'available': False, 'reason': 'source_unavailable'})
        if parsed.path.startswith('/api/'):
            return self.respond(404, {'available': False})
        return self.static(parsed.path)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == '__main__':
    Server((HOST, PORT), Handler).serve_forever()

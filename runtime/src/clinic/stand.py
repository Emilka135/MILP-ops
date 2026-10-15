"""Faithful HTTP facade around MILP-ops' /clients + /visits mock, not invented appointments API."""

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.mis.mock_stand import MockStand
from app.types import MisError

from .crm import now


def make_stand_server(host, port, stand=None):
    stand = stand or MockStand(
        os.environ.get("STAND_STATE", "var/demo-stand.json"),
        clock=lambda: now().replace(tzinfo=None),
    )
    token = os.environ.get("STAND_TOKEN", "demo-token")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def dispatch(self):
            if self.path == "/health":
                return self.send(200, {"status": "ok", "mode": "demo"})
            if self.headers.get("Authorization") != f"Bearer {token}":
                return self.send(
                    401, {"code": "НЕ_АВТОРИЗОВАН", "message": "Неверный токен"}
                )
            u = urllib.parse.urlsplit(self.path)
            if not u.path.startswith("/api/v1/"):
                return self.send(404, {})
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n < 0 or n > 1024 * 1024:
                    return self.send(413, {})
                body = json.loads(self.rfile.read(n)) if n else None
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
                status, result = stand.handle(
                    self.command, u.path[len("/api/v1") :], query, body
                )
                self.send(status, result)
            except MisError as exc:
                self.send(exc.status, {"code": exc.code, "message": exc.message})
            except (ValueError, TypeError):
                self.send(400, {"code": "НЕВЕРНЫЙ_ЗАПРОС", "message": "Неверный JSON"})

        do_GET = do_POST = do_PATCH = do_DELETE = dispatch

    return ThreadingHTTPServer((host, port), Handler)

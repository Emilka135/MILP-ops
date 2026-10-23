"""Minimal HTTP wrapper that exposes one module adapter as a service (stdlib only).

    POST /v1/<op>   JSON body = args   →  {"status": "...", "payload": {...}, "service_ms": 1.2}
    GET  /health    →  {"status": "ok", "module": "crm", "uptime_s": 12.3}
    GET  /metrics   →  Prometheus text (requests, errors, latency buckets)

Binary payloads (TTS PCM) are base64-encoded under ``pcm_b64`` and ``pcm`` is removed.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

Handler = Callable[[str, dict], tuple[str, dict]]


class _Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.errors = 0
        self.buckets = {0.005: 0, 0.01: 0, 0.025: 0, 0.05: 0, 0.1: 0, 0.25: 0, 0.5: 0, 1.0: 0, 2.5: 0, 5.0: 0, float("inf"): 0}
        self.total = 0.0

    def observe(self, seconds: float, error: bool) -> None:
        with self.lock:
            self.requests += 1
            self.errors += int(error)
            self.total += seconds
            for b in self.buckets:
                if seconds <= b:
                    self.buckets[b] += 1

    def render(self, module: str) -> str:
        with self.lock:
            lines = [f'module_requests_total{{module="{module}"}} {self.requests}', f'module_errors_total{{module="{module}"}} {self.errors}',
                     f'module_latency_seconds_sum{{module="{module}"}} {self.total:.6f}', f'module_latency_seconds_count{{module="{module}"}} {self.requests}']
            for b, n in self.buckets.items():
                le = "+Inf" if b == float("inf") else f"{b}"
                lines.append(f'module_latency_seconds_bucket{{module="{module}",le="{le}"}} {n}')
        return "\n".join(lines) + "\n"


def _jsonable(payload: dict) -> dict:
    out = {}
    for k, v in payload.items():
        if isinstance(v, (bytes, bytearray)):
            out[k + "_b64"] = base64.b64encode(v).decode("ascii")
        elif hasattr(v, "__dict__") and not isinstance(v, (str, int, float, bool, list, dict)):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def make_server(module: str, handler: Handler, host: str = "0.0.0.0", port: int = 8080, ready: Callable[[], bool] = lambda: True) -> ThreadingHTTPServer:  # nosec B104  # containers listen on all interfaces by design
    metrics = _Metrics()
    started = time.monotonic()

    class H(BaseHTTPRequestHandler):
        server_version = f"calls-{module}/1.0"

        def log_message(self, fmt: str, *args: object) -> None:  # quiet
            return

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/health", "/livez"):
                self._send(200, json.dumps({"status": "ok", "module": module, "uptime_s": round(time.monotonic() - started, 1)}).encode())
            elif self.path == "/readyz":
                ok = ready()
                self._send(200 if ok else 503, json.dumps({"ready": ok, "module": module}).encode())
            elif self.path == "/metrics":
                self._send(200, metrics.render(module).encode(), "text/plain")
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/v1/"):
                self._send(404, b'{"error":"not found"}')
                return
            op = self.path[4:].split("?", 1)[0]
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0 or n > 3 * 1024 * 1024:
                    self._send(413, b'{"error":"body too large"}')
                    return
                args = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(args, dict):
                    raise ValueError("object required")
            except (json.JSONDecodeError, ValueError):
                self._send(400, b'{"error":"bad json"}')
                return
            t0 = time.perf_counter()
            try:
                status, payload = ("ok", {"module": module}) if op == "health" else handler(op, args)
            except Exception as exc:  # noqa: BLE001
                status, payload = "error", {"error": f"{type(exc).__name__}: {exc}"}
            dt = time.perf_counter() - t0
            metrics.observe(dt, status == "error")
            body = json.dumps({"status": status, "payload": _jsonable(payload), "service_ms": round(dt * 1000, 3)}, ensure_ascii=False, default=str).encode("utf-8")
            self._send(200 if status != "error" else 500, body)

    return ThreadingHTTPServer((host, port), H)

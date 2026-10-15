"""A stand-in for the organisers' stand: the same contract as :class:`calls.crm.remote.EndpointMap` defaults.

Used by the compose e2e, the test-case runner (``tools/run_cases.py``) and the unit tests to **verify the
output of the solution** – which records really arrived through the API, in which order, with which body.

* ``Authorization: Bearer <token>`` (``STAND_TOKEN``, default ``team-token``; empty = no auth)
* 10 requests per minute per token (``STAND_RPM``) → ``429`` + ``Retry-After`` – exercises our TokenBucket
* appointments: ``POST /api/v1/appointments``, ``GET /api/v1/appointments[?date=&patient_id=]``,
  ``GET|PATCH|DELETE /api/v1/appointments/{id}``, ``POST /api/v1/appointments/{id}/confirm``
* operator requests: ``POST|GET /api/v1/operator-requests``
* reference data: ``GET /api/v1/doctors``, ``GET /api/v1/patients`` (when seeded from a registry)
* admin (not rate-limited, no auth): ``GET /health``, ``GET /openapi.json``, ``GET /admin/log`` (every request
  received), ``POST /admin/reset``

Run: ``python -m calls.services stand --port 8087`` or ``python -m calls.crm.fake_stand``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from calls.crm.registry import MockRegistry

_ID_RE = re.compile(r"^/api/v1/appointments/([^/]+)(/confirm)?$")


class FakeStand:
    def __init__(self, token: str = "team-token", rpm: int = 10, state_path: Optional[str] = None, reg: Optional[MockRegistry] = None) -> None:  # noqa: S107  # demo token
        self.token, self.rpm, self.state_path = token, rpm, state_path
        self._lock = threading.RLock()
        self._stamps: deque[float] = deque()
        self.appointments: dict[str, dict] = {}
        self.operator_requests: dict[str, dict] = {}
        self.log: list[dict] = []
        self.rejected_429 = 0
        self._seq = 0
        self.reg = reg
        if state_path and Path(state_path).exists():
            data = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.appointments = data.get("appointments", {})
            self.operator_requests = data.get("operator_requests", {})
            self._seq = int(data.get("seq", 0))

    # ---- helpers ----------------------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def _save(self) -> None:
        if self.state_path:
            Path(self.state_path).write_text(json.dumps({"appointments": self.appointments, "operator_requests": self.operator_requests, "seq": self._seq}, ensure_ascii=False, indent=1), encoding="utf-8")

    def _rate_ok(self) -> tuple[bool, float]:
        now = time.monotonic()
        while self._stamps and now - self._stamps[0] >= 60.0:
            self._stamps.popleft()
        if len(self._stamps) >= self.rpm:
            return False, 60.0 - (now - self._stamps[0])
        self._stamps.append(now)
        return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self.appointments.clear()
            self.operator_requests.clear()
            self.log.clear()
            self._stamps.clear()
            self.rejected_429 = 0
            self._seq = 0
            self._save()

    # ---- the API ----------------------------------------------------------------------------------

    def handle(self, method: str, raw_path: str, body: Optional[dict], auth: Optional[str]) -> tuple[int, dict]:
        """Returns ``(status, json)``; pure function of the state so it is unit-testable without sockets."""
        url = urlsplit(raw_path)
        path, qs = url.path.rstrip("/") or "/", parse_qs(url.query)
        if path == "/health":
            return 200, {"status": "ok", "module": "stand", "appointments": len(self.appointments), "operator_requests": len(self.operator_requests)}
        if path == "/openapi.json":
            return 200, OPENAPI
        if path == "/metrics":  # Prometheus text, like every other module
            with self._lock:
                booked = sum(1 for a in self.appointments.values() if a["status"] == "booked")
                return 200, {"_text": (f"stand_appointments_total {len(self.appointments)}\nstand_appointments_booked {booked}\n"
                                       f"stand_operator_requests_total {len(self.operator_requests)}\nstand_requests_total {len(self.log)}\n"
                                       f"stand_rejected_429_total {self.rejected_429}\n")}
        if path == "/admin/log":
            with self._lock:
                return 200, {"items": list(self.log), "rejected_429": self.rejected_429}
        if path == "/admin/reset" and method == "POST":
            self.reset()
            return 200, {"status": "reset"}
        if path == "/admin/appointments" and method == "POST":  # pre-existing (registrar-made) record with a given id
            rec = {"status": "booked", "confirmed": False, "source": "registrar", "created_at": _now(), "updated_at": _now(), **(body or {})}
            rec["id"] = str(rec.get("id") or self._next_id("APT"))
            with self._lock:
                self.appointments[rec["id"]] = rec
                self._save()
            return 201, rec
        if self.token and auth != f"Bearer {self.token}":
            return 401, {"error": "invalid or missing bearer token"}
        with self._lock:
            ok, wait = self._rate_ok()
            if not ok:
                self.rejected_429 += 1
                self.log.append({"ts": _now(), "method": method, "path": raw_path, "status": 429})
                return 429, {"error": "rate limit: 10 requests per minute", "retry_after": round(wait, 1)}
            status, out = self._dispatch(method, path, qs, body or {})
            if method != "GET":
                self.log.append({"ts": _now(), "method": method, "path": raw_path, "body": body, "status": status, "response": out})
                self._save()
            return status, out

    def _dispatch(self, method: str, path: str, qs: dict, body: dict) -> tuple[int, dict]:
        if path == "/api/v1/appointments":
            if method == "POST":
                return self._create(body)
            if method == "GET":
                items = list(self.appointments.values())
                if qs.get("date"):
                    items = [a for a in items if str(a.get("starts_at", ""))[:10] == qs["date"][0]]
                if qs.get("patient_id"):
                    items = [a for a in items if str(a.get("patient_id")) == qs["patient_id"][0]]
                if qs.get("status"):
                    items = [a for a in items if a.get("status") == qs["status"][0]]
                return 200, {"items": items}
            return 405, {"error": "method not allowed"}
        m = _ID_RE.match(path)
        if m:
            aid, confirm = m.group(1), bool(m.group(2))
            a = self.appointments.get(aid)
            if a is None:
                return 404, {"error": f"appointment {aid} not found"}
            if confirm:
                if method != "POST":
                    return 405, {"error": "method not allowed"}
                if a["status"] != "booked":
                    return 409, {"error": f"appointment {aid} is {a['status']}"}
                a["confirmed"], a["updated_at"] = True, _now()
                return 200, a
            if method == "GET":
                return 200, a
            if method == "DELETE":
                if a["status"] == "cancelled":
                    return 409, {"error": "already cancelled"}
                a["status"], a["updated_at"] = "cancelled", _now()
                return 200, a
            if method in ("PATCH", "PUT"):
                if a["status"] != "booked":
                    return 409, {"error": f"appointment {aid} is {a['status']}"}
                new_start = body.get("starts_at") or a["starts_at"]
                new_doc = body.get("doctor_id") or a.get("doctor_id")
                clash = self._clash(new_doc, new_start, exclude=aid)
                if clash:
                    return 409, {"error": f"slot taken by {clash}"}
                a.setdefault("history", []).append({"starts_at": a["starts_at"], "doctor_id": a.get("doctor_id"), "slot_id": a.get("slot_id")})
                a["starts_at"], a["doctor_id"] = new_start, new_doc
                a["slot_id"] = body.get("slot_id", a.get("slot_id"))
                a["confirmed"], a["updated_at"], a["rescheduled"] = False, _now(), True
                return 200, a
            return 405, {"error": "method not allowed"}
        if path == "/api/v1/operator-requests":
            if method == "POST":
                rid = self._next_id("OR")
                rec = {"id": rid, "created_at": _now(), **body}
                self.operator_requests[rid] = rec
                return 201, rec
            if method == "GET":
                return 200, {"items": list(self.operator_requests.values())}
            return 405, {"error": "method not allowed"}
        if path == "/api/v1/doctors" and method == "GET":
            return 200, {"items": self.reg.doctors() if self.reg else []}
        if path == "/api/v1/patients" and method == "GET":
            items = self.reg.export_patients() if self.reg else []
            return 200, {"items": [{k: v for k, v in p.items() if k in ("id", "last_name", "first_name", "middle_name", "birth_date", "card_no")} for p in items]}
        return 404, {"error": f"no route {method} {path}"}

    def _clash(self, doctor_id: object, starts_at: object, exclude: Optional[str] = None) -> Optional[str]:
        for aid, a in self.appointments.items():
            if aid != exclude and a["status"] == "booked" and str(a.get("doctor_id")) == str(doctor_id) and a["starts_at"] == starts_at:
                return aid
        return None

    def _create(self, body: dict) -> tuple[int, dict]:
        missing = [k for k in ("patient_id", "starts_at") if body.get(k) in (None, "")]
        if missing:
            return 422, {"error": f"missing fields: {', '.join(missing)}"}
        if body.get("doctor_id") is not None and self._clash(body["doctor_id"], body["starts_at"]):
            return 409, {"error": "slot already booked"}
        aid = self._next_id("APT")
        rec = {"id": aid, "status": "booked", "confirmed": False, "created_at": _now(), "updated_at": _now(),
               "patient_id": body.get("patient_id"), "doctor_id": body.get("doctor_id"), "slot_id": body.get("slot_id"),
               "starts_at": body.get("starts_at"), "source": body.get("source"), "call_id": body.get("call_id")}
        self.appointments[aid] = rec
        return 201, rec

    # ---- server -------------------------------------------------------------------------------------

    def make_server(self, host: str = "0.0.0.0", port: int = 8087) -> ThreadingHTTPServer:  # nosec B104  # containers bind all interfaces
        stand = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "calls-fake-stand/1.0"

            def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401 - quiet
                return

            def _run(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body: Optional[dict] = None
                if raw:
                    try:
                        body = json.loads(raw)
                    except json.JSONDecodeError:
                        self._send(400, {"error": "invalid JSON"})
                        return
                status, out = stand.handle(self.command, self.path, body, self.headers.get("Authorization"))
                self._send(status, out)

            def _send(self, status: int, out: dict) -> None:
                if "_text" in out:  # /metrics
                    data = str(out["_text"]).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                data = json.dumps(out, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                if status == 429:
                    self.send_header("Retry-After", str(int(out.get("retry_after", 60)) + 1))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _run

        srv = ThreadingHTTPServer((host, port), Handler)
        srv.daemon_threads = True
        return srv

    def start_in_thread(self, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
        srv = self.make_server(host, port)
        threading.Thread(target=srv.serve_forever, name="fake-stand", daemon=True).start()
        return srv, f"http://{host}:{srv.server_address[1]}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


OPENAPI: dict = {
    "openapi": "3.0.3",
    "info": {"title": "Demo stand (assumed contract)", "version": "1.0", "description": "Replace with the organisers' spec; map it in config/stand.endpoints.json"},
    "paths": {
        "/api/v1/appointments": {
            "post": {"summary": "book", "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/AppointmentIn"}}}}, "responses": {"201": {"description": "created"}}},
            "get": {"summary": "list", "parameters": [{"name": "date", "in": "query"}, {"name": "patient_id", "in": "query"}], "responses": {"200": {"description": "items"}}},
        },
        "/api/v1/appointments/{id}": {
            "patch": {"summary": "reschedule", "responses": {"200": {"description": "updated"}}},
            "delete": {"summary": "cancel", "responses": {"200": {"description": "cancelled"}}},
            "get": {"summary": "read", "responses": {"200": {"description": "record"}}},
        },
        "/api/v1/appointments/{id}/confirm": {"post": {"summary": "confirm", "responses": {"200": {"description": "confirmed"}}}},
        "/api/v1/operator-requests": {
            "post": {"summary": "transfer to operator", "responses": {"201": {"description": "created"}}},
            "get": {"summary": "list", "responses": {"200": {"description": "items"}}},
        },
        "/api/v1/doctors": {"get": {"responses": {"200": {"description": "items"}}}},
        "/api/v1/patients": {"get": {"responses": {"200": {"description": "items"}}}},
    },
    "components": {
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "AppointmentIn": {"type": "object", "required": ["patient_id", "starts_at"], "properties": {
                "patient_id": {"type": "integer"}, "doctor_id": {"type": "integer"}, "slot_id": {"type": "integer"},
                "starts_at": {"type": "string", "format": "date-time"}, "source": {"type": "string"}, "call_id": {"type": "string"}}},
            "OperatorRequestIn": {"type": "object", "properties": {
                "call_id": {"type": "string"}, "reason": {"type": "string"}, "patient_id": {"type": "integer"}, "booking_id": {"type": "string"},
                "constraints": {"type": "object"}, "user_state": {"type": "string"}, "summary": {"type": "string"}}},
        },
    },
    "security": [{"bearer": []}],
    "x-rate-limit": "10 requests per minute per token",
}


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Fake demo stand (appointments + operator requests API, 10 RPM)")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))  # nosec B104  # container
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8087")))
    ap.add_argument("--token", default=os.environ.get("STAND_TOKEN", "team-token"))
    ap.add_argument("--rpm", type=int, default=int(os.environ.get("STAND_RPM", "10")))
    ap.add_argument("--state", default=os.environ.get("STAND_STATE") or None)
    ap.add_argument("--seed-demo", action="store_true", default=os.environ.get("CRM_SEED_DEMO", "0") == "1")
    ns = ap.parse_args(argv)
    reg = None
    if ns.seed_demo:
        from datetime import date

        from calls.crm.registry import seed_demo

        reg = MockRegistry(":memory:")
        seed_demo(reg, date.today())
    stand = FakeStand(ns.token, ns.rpm, ns.state, reg)
    srv = stand.make_server(ns.host, ns.port)
    print(f"fake stand on http://{ns.host}:{ns.port} (token={'<none>' if not ns.token else ns.token[:3] + '…'}, {ns.rpm} RPM)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

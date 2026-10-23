"""Test-case runner: **input → output** for the hackathon's open cases.

A case is a JSON document (``cases/*.json``, one file = one list of cases)::

    {
      "id": "inbound_book_dtmf",
      "title": "Запись к терапевту клавишами, звонящий известен по номеру",
      "flow": "inbound",                      # inbound | reminder (outbound)
      "caller_phone": "79035551234",
      "today": "2026-10-14",                  # the mirror's "today" (the demo schedule starts here)
      "setup": {"bookings": [{"remote_id": "APT-100", "patient_id": 1, "doctor": 2, "starts_at": "2026-10-15T10:00"}]},
      "reminder_for": "APT-100",              # outbound: build the reminder vars from this booking
      "steps": [                              # the INPUT – what the caller does
        {"wait": "inbound.welcome", "dtmf": "1"},
        {"wait": "inbound.id_birth_check", "digits": "12051980"},
        {"wait": "inbound.book_start", "say": "хочу к терапевту"},
        {"wait": "inbound.listen_free", "timeout": true},
        {"silence": true}
      ],
      "expect": {                             # the OUTPUT – what must be true afterwards
        "status": "booked",                   # call outcome
        "stand": [{"method": "POST", "path": "/api/v1/appointments", "body": {"patient_id": 1}}],
        "stand_exact": true,                  # no other writes reached the stand (default true)
        "transfer": false,                    # or true / "human_only" (reason)
        "sms_contains": "Иван Иванович",
        "vars": {"specialty": "терапевт"},   # subset of the final call vars
        "mirror": {"APT-100": "cancelled"}    # status of a pre-existing booking in the mirror
      }
    }

``run_case`` executes it against an **in-process** orchestrator wired to the :class:`~calls.crm.fake_stand.FakeStand`
(or against a deployed stack when ``--orchestrator`` / ``--stand`` URLs are given to ``tools/run_cases.py``) and
compares the stand's request log with ``expect.stand`` – that is exactly how the organisers' autoruns judge us.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from calls.crm.fake_stand import FakeStand
from calls.crm.registry import MockRegistry, seed_demo
from calls.services.netutil import open_http, post_json
from calls.crm.remote import EndpointMap, HttpRegistry, TokenBucket
from calls.crm.stand import StandSync
from calls.scenario.engine import Event

# The in-process LocalEnv needs the orchestrator (→ NLU → numpy); RemoteEnv drives a running stack over HTTP and must work
# on a bare interpreter (the compose e2e job has no project deps installed), so those imports are deferred.


def _load_flows(path: Path) -> dict:
    from calls.scenario.dsl import load_flows

    return load_flows(path)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TODAY = date(2026, 10, 14)


@dataclass
class CaseResult:
    case_id: str
    title: str
    ok: bool
    status: str
    expected_status: Optional[str]
    writes: list[dict]
    expected_writes: list[dict]
    problems: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    transfer_reason: Optional[str] = None
    sms: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)

    def row(self) -> dict:
        return {"id": self.case_id, "ok": self.ok, "status": self.status, "expected": self.expected_status,
                "writes": [f"{w['method']} {w['path']}" for w in self.writes], "problems": self.problems, "ms": round(self.duration_s * 1000)}


def load_cases(paths: list[Path | str] | None = None) -> list[dict]:
    paths = paths or sorted((ROOT / "cases").glob("*.json"))
    out: list[dict] = []
    for p in paths:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        items = data["cases"] if isinstance(data, dict) else data
        for c in items:
            c.setdefault("_file", str(p))
            out.append(c)
    return out


# ---- environment --------------------------------------------------------------------------------


class LocalEnv:
    """Fresh mirror + fake stand + orchestrator per case (no state leaks between cases)."""

    def __init__(self, case: dict, flows: Optional[dict] = None, rpm: int = 600) -> None:
        self.today = date.fromisoformat(case.get("today", DEFAULT_TODAY.isoformat()))
        self.stand = FakeStand("team-token", rpm=rpm)  # the real stand allows 10/min; cases must not depend on waiting
        self.srv, self.url = self.stand.start_in_thread()
        now = datetime.fromisoformat(case.get("now", f"{self.today.isoformat()}T09:00"))  # frozen clock: cases are deterministic on any date
        self.reg = MockRegistry(":memory:", now=now)
        seed_demo(self.reg, self.today)
        apply_setup(self.reg, case.get("setup") or {}, self.stand)
        client = HttpRegistry(self.url, "team-token", EndpointMap(), TokenBucket(rpm, 60.0))
        self.sync = StandSync(self.reg, client).start()
        from calls.orchestrator.app import Orchestrator

        self.orch = Orchestrator(flows or _load_flows(ROOT / "scenarios"), self.reg, today=lambda: self.today, stand=self.sync)

    def close(self, final: Optional[dict] = None) -> None:
        self.sync.stop(drain=True, timeout=5.0)
        self.srv.shutdown()
        self.srv.server_close()

    # the three things a case needs: drive the call, flush the outbox, read the stand's log
    def handle(self, op: str, args: dict) -> dict:
        return self.orch.handle(op, args)[1]

    def flush(self, timeout: float = 10.0) -> None:
        self.orch.handle("stand.flush", {"timeout": timeout})

    def stand_log(self) -> list[dict]:
        return [e for e in self.stand.log if e.get("status") != 429]

    def stand_appointment(self, remote_id: str) -> Optional[dict]:
        return self.stand.appointments.get(remote_id)

    def reminder_vars(self, remote_id: str) -> dict:
        status, payload = self.orch.stack.crm.handle("reminder_vars", {"remote_id": remote_id})
        if status != "ok":
            raise KeyError(f"no booking with stand id {remote_id}")
        return {**payload, **self.orch.clinic}

    def inject_timeout(self, call_id: str) -> None:
        self.orch.handle("call.timeout", {"call_id": call_id})


class RemoteEnv:
    """The same three primitives against a deployed stack (``tools/run_cases.py --orchestrator … --stand …``).

    The stand must be our fake stand (its ``/admin`` endpoints provide the request log and pre-existing records);
    ``crm`` is the service that owns the outbox when the modules run as separate containers.
    """

    def __init__(self, case: dict, orchestrator: str, stand: str, crm: Optional[str] = None, token: str = "team-token") -> None:  # noqa: S107  # fake stand's demo token
        self.base, self.stand_url, self.crm_url, self.token = orchestrator.rstrip("/"), stand.rstrip("/"), (crm or orchestrator).rstrip("/"), token
        self.today = date.fromisoformat(case.get("today", DEFAULT_TODAY.isoformat()))
        self.flush()
        self._touched: list[int] = []  # mirror booking ids to cancel afterwards (a patient's "my booking" is the nearest upcoming one)
        setup_bookings = (case.get("setup") or {}).get("bookings", [])
        # the premise of a case with pre-existing bookings is "the patient has exactly these" – on a shared dev stack earlier
        # calls (e.g. the e2e booking) may have left others behind: cancel them first (never done against a real stand)
        for pid in {int(b["patient_id"]) for b in setup_bookings}:
            for _ in range(10):
                r = self._post(f"{self.crm_url}/v1/my_booking", {"patient_id": pid})
                if r.get("status") != "ok":
                    break
                self._post(f"{self.crm_url}/v1/cancel", {"booking_id": r["payload"]["booking_id"], "call_id": "case-prep"})
        self.flush()
        self._offset = len(self._all_log())  # the deployed stack keeps state between cases: judge only this case's writes
        for b in setup_bookings:
            r = self._post(f"{self.crm_url}/v1/import_booking", {"remote_id": b["remote_id"], "patient_id": b["patient_id"], "doctor": b.get("doctor", 1),
                                                                 "starts_at": b["starts_at"], "confirmed": b.get("confirmed", False)})
            if r.get("status") != "ok":
                raise RuntimeError(f"import_booking failed: {r}")
            self._touched.append(int(r["payload"]["booking_id"]))
            self._post(f"{self.stand_url}/admin/appointments", {"id": str(b["remote_id"]), "patient_id": b["patient_id"], "doctor_id": b.get("doctor"),
                                                                "starts_at": b["starts_at"], "confirmed": b.get("confirmed", False)})

    def _post(self, url: str, payload: dict) -> dict:
        return post_json(url, payload, timeout=30.0)

    def _get(self, url: str, auth: bool = False) -> dict:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"} if auth else {})
        with open_http(req, timeout=30.0) as resp:
            return json.loads(resp.read())

    def handle(self, op: str, args: dict) -> dict:
        return self._post(f"{self.base}/v1/{op}", args).get("payload", {})

    def flush(self, timeout: float = 10.0) -> None:
        self._post(f"{self.crm_url}/v1/stand.flush", {"timeout": timeout})

    def _all_log(self) -> list[dict]:
        return [e for e in self._get(f"{self.stand_url}/admin/log").get("items", []) if e.get("status") != 429]

    def stand_log(self) -> list[dict]:
        return self._all_log()[self._offset:]

    def stand_appointment(self, remote_id: str) -> Optional[dict]:
        try:
            return self._get(f"{self.stand_url}/api/v1/appointments/{remote_id}", auth=True)
        except Exception:  # noqa: BLE001 - 404 → None
            return None

    def reminder_vars(self, remote_id: str) -> dict:
        r = self._post(f"{self.crm_url}/v1/reminder_vars", {"remote_id": remote_id})
        if r.get("status") != "ok":
            raise KeyError(f"no booking with stand id {remote_id}")
        return r["payload"]

    def inject_timeout(self, call_id: str) -> None:
        self.handle("call.timeout", {"call_id": call_id})

    def close(self, final: Optional[dict] = None) -> None:
        """Leave the shared mirror as we found it: cancel the bookings this case created or imported."""
        ids = list(self._touched)
        if final and str(final.get("status")) in ("booked", "rescheduled", "confirmed") and (final.get("vars") or {}).get("booking_id"):
            ids.append(int(final["vars"]["booking_id"]))
        for bid in ids:
            try:
                self._post(f"{self.crm_url}/v1/cancel", {"booking_id": bid, "call_id": "case-cleanup"})
            except urllib.error.HTTPError:  # "booking is cancelled/rescheduled" → the service answers 500 with status=error: nothing to do
                pass
            except Exception as exc:  # noqa: BLE001 - network: report, do not fail the case
                print(f"cleanup: booking {bid} not cancelled ({exc})")
        self.flush()


def apply_setup(reg: MockRegistry, setup: dict, stand: Optional[FakeStand] = None) -> None:
    """Pre-existing bookings (made by registrars) exist both in the stand and in the mirror, with the same ids."""
    for row in setup.get("patients", []):
        reg.load_patients_rows([row])
    for b in setup.get("bookings", []):
        when = datetime.fromisoformat(b["starts_at"])
        bid = reg.import_booking(str(b["remote_id"]), int(b["patient_id"]), b.get("doctor", 1), when, bool(b.get("confirmed", False)))
        if stand is not None:
            bk = reg.booking(bid)
            assert bk is not None
            stand.appointments[str(b["remote_id"])] = {
                "id": str(b["remote_id"]), "status": "booked", "confirmed": bool(b.get("confirmed", False)), "patient_id": int(b["patient_id"]),
                "doctor_id": bk.doctor_id, "slot_id": bk.slot_id, "starts_at": when.isoformat(), "source": "registrar", "created_at": "setup", "updated_at": "setup",
            }


# ---- execution ----------------------------------------------------------------------------------


def drive(handle: Callable[[str, dict], dict], call_id: str, steps: list[dict], inject: Optional[Callable[[str, Event], None]] = None, timeout: float = 10.0) -> dict:
    def state() -> dict:
        return handle("call.state", {"call_id": call_id})

    def wait(pred: Callable[[dict], bool], what: str) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = state()
            if pred(s):
                return s
            if s.get("finished"):
                raise AssertionError(f"call finished ({s.get('status')}) while waiting for {what}; errors={s.get('errors')}")
            time.sleep(0.02)
        s = state()
        raise AssertionError(f"timeout waiting for {what}; state={s.get('state')} errors={s.get('errors')}")

    for step in steps:
        want = step.get("wait")
        if want and want != "finished":
            wait(lambda s, w=want: s["state"] == w, want)
        elif want == "finished":
            wait(lambda s: bool(s["finished"]), "finished")
        if "dtmf" in step:
            handle("call.dtmf", {"call_id": call_id, "digit": str(step["dtmf"])})
        elif "digits" in step:
            for d in str(step["digits"]):
                handle("call.dtmf", {"call_id": call_id, "digit": d})
        elif "say" in step:
            handle("call.speech", {"call_id": call_id, "text": str(step["say"])})
        elif "partial" in step:
            handle("call.partial", {"call_id": call_id, "text": str(step["partial"]), "at_ms": step.get("at_ms", 800)})
        elif step.get("silence"):
            handle("call.silence", {"call_id": call_id})
        elif step.get("timeout"):  # "the caller pressed nothing": fire the state's no-input timer now
            if inject is not None:
                inject(call_id, Event("timeout"))
            else:
                handle("call.timeout", {"call_id": call_id})
        elif step.get("hangup"):
            handle("call.hangup", {"call_id": call_id})
        if step.get("sleep") and not step.get("timeout"):
            time.sleep(float(step["sleep"]))
    return wait(lambda s: bool(s["finished"]), "finished")


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(_subset(e, a) for e, a in zip(expected, actual))
    if isinstance(expected, str) and expected.startswith("~"):  # regex
        return re.search(expected[1:], str(actual)) is not None
    return expected == actual or str(expected) == str(actual)


def _path_matches(expected: str, actual: str) -> bool:
    pattern = "^" + re.sub(r"\\\{[a-z_]+\\\}", r"[^/?]+", re.escape(expected)) + r"(\?.*)?$"
    return re.match(pattern, actual) is not None


def check_expectations(case: dict, final: dict, writes: list[dict], mirror_status: Callable[[str], Optional[str]]) -> list[str]:
    exp = case.get("expect") or {}
    problems: list[str] = []
    if "status" in exp and final.get("status") != exp["status"]:
        problems.append(f"call status {final.get('status')!r} != {exp['status']!r}")
    if "transfer" in exp:
        t = final.get("transfer")
        if exp["transfer"] is False and t:
            problems.append(f"unexpected transfer to operator ({t.get('reason') if isinstance(t, dict) else t})")
        elif exp["transfer"] is True and not t:
            problems.append("expected a transfer to operator")
        elif isinstance(exp["transfer"], str) and (not t or t.get("reason") != exp["transfer"]):
            problems.append(f"transfer reason {t.get('reason') if t else None!r} != {exp['transfer']!r}")
    if "sms_contains" in exp:
        texts = [str(s.get("text", s)) for s in (final.get("sms") or [])]
        if not any(exp["sms_contains"] in t for t in texts):
            problems.append(f"no SMS containing {exp['sms_contains']!r} (got {texts})")
    if "sms" in exp and bool(final.get("sms")) != bool(exp["sms"]):
        problems.append(f"sms sent={bool(final.get('sms'))}, expected {bool(exp['sms'])}")
    for k, v in (exp.get("vars") or {}).items():
        if not _subset(v, (final.get("vars") or {}).get(k)):
            problems.append(f"var {k}={final.get('vars', {}).get(k)!r} != {v!r}")
    expected_writes = exp.get("stand")
    if expected_writes is not None:
        if exp.get("stand_exact", True) and len(writes) != len(expected_writes):
            problems.append(f"{len(writes)} stand writes, expected {len(expected_writes)}: " + ", ".join(f"{w['method']} {w['path']}" for w in writes))
        for i, ew in enumerate(expected_writes):
            if i >= len(writes):
                problems.append(f"missing stand write #{i + 1}: {ew.get('method')} {ew.get('path')}")
                continue
            w = writes[i]
            if ew.get("method") and ew["method"].upper() != w["method"]:
                problems.append(f"write #{i + 1}: method {w['method']} != {ew['method']}")
            if ew.get("path") and not _path_matches(ew["path"], w["path"]):
                problems.append(f"write #{i + 1}: path {w['path']} !~ {ew['path']}")
            if ew.get("body") is not None and not _subset(ew["body"], w.get("body") or {}):
                problems.append(f"write #{i + 1}: body {json.dumps(w.get('body'), ensure_ascii=False)} lacks {json.dumps(ew['body'], ensure_ascii=False)}")
            if ew.get("status") and int(ew["status"]) != int(w.get("status", 0)):
                problems.append(f"write #{i + 1}: HTTP {w.get('status')} != {ew['status']}")
            elif not ew.get("status") and int(w.get("status", 0)) >= 400:
                problems.append(f"write #{i + 1}: the stand answered HTTP {w.get('status')}")
    for rid, want in (exp.get("mirror") or {}).items():
        got = mirror_status(rid)
        if got != want:
            problems.append(f"stand record {rid}: status {got!r} != {want!r}")
    return problems


def run_case(case: dict, flows: Optional[dict] = None, env_factory: Optional[Callable[[dict], Any]] = None, timeout: float = 10.0) -> CaseResult:
    t0 = time.time()
    env = (env_factory or (lambda c: LocalEnv(c, flows)))(case)
    call_id = f"case-{case['id']}"
    final: dict = {}
    try:
        vars = dict(case.get("vars") or {})
        if case.get("reminder_for"):
            vars = {**env.reminder_vars(str(case["reminder_for"])), **vars}
        env.handle("call.start", {"call_id": call_id, "flow": case.get("flow", "inbound"), "caller_phone": case.get("caller_phone", ""), "vars": vars})
        try:
            final = drive(env.handle, call_id, case.get("steps", []), inject=lambda cid, _ev: env.inject_timeout(cid), timeout=timeout)
        except AssertionError as exc:
            final = env.handle("call.state", {"call_id": call_id})
            final["_drive_error"] = str(exc)
        # fire-and-forget work (operator ticket, SMS) finishes a few ms after the call → settle, then flush the outbox
        deadline = time.time() + 5.0
        last = None
        while time.time() < deadline:
            env.flush(timeout=5.0)
            snapshot = (len(env.stand_log()), len(final.get("sms") or []), bool(final.get("transfer")))
            if snapshot == last:
                break
            last = snapshot
            time.sleep(0.1)
            final = env.handle("call.state", {"call_id": call_id})
        writes = env.stand_log()
        problems = check_expectations(case, final, writes, lambda rid: (env.stand_appointment(rid) or {}).get("status"))
        if final.get("_drive_error"):
            problems.insert(0, final["_drive_error"])
        if final.get("errors"):
            problems.append(f"call errors: {final['errors']}")
        return CaseResult(case["id"], case.get("title", ""), not problems, str(final.get("status")), (case.get("expect") or {}).get("status"), writes,
                          (case.get("expect") or {}).get("stand") or [], problems, time.time() - t0,
                          (final.get("transfer") or {}).get("reason") if isinstance(final.get("transfer"), dict) else None,
                          [str(s.get("text", s)) for s in (final.get("sms") or [])], list(case.get("steps", [])))
    finally:
        env.close(final)


def run_all(cases: list[dict], flows: Optional[dict] = None, only: Optional[str] = None, env_factory: Optional[Callable[[dict], Any]] = None,
            timeout: float = 10.0) -> list[CaseResult]:
    flows = None if env_factory else (flows or _load_flows(ROOT / "scenarios"))
    out = []
    for c in cases:
        if only and only not in c["id"]:
            continue
        out.append(run_case(c, flows, env_factory=env_factory, timeout=timeout))
    return out


def format_table(results: list[CaseResult]) -> str:
    lines = ["| # | кейс | вход (шаги) | ожидаемый выход | фактический выход | итог |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        exp = ", ".join(f"{w.get('method')} {w.get('path')}" for w in r.expected_writes) or "—"
        got = ", ".join(f"{w['method']} {w['path']} → {w['status']}" for w in r.writes) or "—"
        lines.append(f"| {i} | `{r.case_id}` {r.title} | {_steps_brief(r)} | {r.expected_status or '—'}; {exp} | {r.status}; {got} | {'✅' if r.ok else '❌ ' + '; '.join(r.problems)} |")
    return "\n".join(lines)


def _steps_brief(r: CaseResult) -> str:
    bits = []
    for st in r.steps:
        if "dtmf" in st:
            bits.append(f"[{st['dtmf']}]")
        elif "digits" in st:
            bits.append(f"[{st['digits']}]")
        elif "say" in st:
            bits.append(f"«{st['say']}»")
        elif "partial" in st:
            bits.append(f"…«{st['partial']}»")
        elif st.get("silence"):
            bits.append("тишина")
        elif st.get("timeout"):
            bits.append("⏱")
        elif st.get("hangup"):
            bits.append("☎︎✕")
    return " → ".join(bits) or "—"

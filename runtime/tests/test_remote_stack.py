"""The compose topology in-process: crm / nlu / sms / operator / tts / asr run as HTTP services on threads,
the orchestrator reaches them only through *_URL (HttpModuleClient).  Same scripted calls as the e2e grinder."""

import threading
import time
from datetime import date

import pytest

from calls.crm.module import CrmModule
from calls.crm.registry import MockRegistry, seed_demo
from calls.notify.sms import MockSmsGateway, SmsModule
from calls.orchestrator.app import Orchestrator
from calls.orchestrator.modules import AsrModule, NluModule, OperatorModule
from calls.scenario.engine import Event
from calls.services.http import make_server
from calls.tts.silero import StubTTS, TtsModule
from tests.conftest import TODAY, wait_until


@pytest.fixture
def remote_stack(flows):
    reg = MockRegistry()
    seed_demo(reg, TODAY)
    gw = MockSmsGateway()
    operator = OperatorModule(reg)
    handlers = {
        "crm": CrmModule(reg, today=lambda: TODAY).handle,
        "nlu": lambda op, a: (lambda st, p: (st, {k: v for k, v in p.items() if k != "outcome"}))(*NluModule().handle(op, a)),
        "sms": SmsModule(gw, lambda v: reg.patient_by_id(int(v["patient_id"])).phone if v.get("patient_id") else None, {"clinic_name": "П", "clinic_phone": "+7"}).handle,
        "operator": operator.handle,
        "tts": TtsModule(StubTTS()).handle,
        "asr": AsrModule().handle,
    }
    servers = {}
    for name, h in handlers.items():
        srv = make_server(name, h, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers[name] = srv
    urls = {name: f"http://127.0.0.1:{srv.server_address[1]}" for name, srv in servers.items()}
    orch = Orchestrator(flows, reg, today=lambda: TODAY, remote=urls)
    try:
        yield orch, reg, gw, operator
    finally:
        orch.shutdown()
        for srv in servers.values():
            srv.shutdown()


def drive(orch, call_id, steps, timeout=10.0):
    """steps: list of (wait_for_state | None, action) where action is ("dtmf", d) | ("digits", s) | ("speech", t) | ("timeout",)."""
    def state():
        return orch.handle("call.state", {"call_id": call_id})[1]

    def wait(pred):
        d = time.time() + timeout
        while time.time() < d:
            s = state()
            if pred(s):
                return s
            time.sleep(0.02)
        raise AssertionError(f"timeout waiting; state={state()['state']} errors={state()['errors']}")

    for want, action in steps:
        if want:
            wait(lambda s, w=want: s["state"] == w)
        if action[0] == "dtmf":
            orch.handle("call.dtmf", {"call_id": call_id, "digit": action[1]})
        elif action[0] == "digits":
            for d in action[1]:
                orch.handle("call.dtmf", {"call_id": call_id, "digit": d})
        elif action[0] == "speech":
            orch.handle("call.speech", {"call_id": call_id, "text": action[1]})
        elif action[0] == "audio":
            orch.handle("call.audio", {"call_id": call_id, "pcm_b64": "", "rate": 8000, "text": action[1]})
        elif action[0] == "timeout":
            orch.get(call_id)._inject(Event("timeout"))
    return wait(lambda s: s["finished"])


def test_dtmf_booking_over_http_modules(remote_stack):
    orch, reg, gw, operator = remote_stack
    orch.handle("call.start", {"call_id": "r1", "flow": "inbound", "caller_phone": "79035551234"})
    s = drive(orch, "r1", [
        ("inbound.welcome", ("dtmf", "1")), ("inbound.id_self_or_other", ("dtmf", "1")), ("inbound.id_birth_check", ("digits", "12051980")),
        ("inbound.book_start", ("dtmf", "1")), ("inbound.book_offer", ("dtmf", "1")),
    ])
    assert s["status"] == "booked" and s["errors"] == []
    assert wait_until(lambda: bool(gw.sent))  # SMS is fire-and-forget over HTTP
    assert gw.sent[0][0] == "79035551234" and gw.sent[0][1].startswith("Иван Иванович")
    b = reg.booking(s["vars"]["booking_id"])
    assert b.patient_id == 1 and b.status == "booked"
    # the TTS clips came back over HTTP with their PCM re-materialised as bytes
    nxt = orch.handle("call.next_prompt", {"call_id": "r1"})[1]
    assert nxt and nxt["pcm_b64"] and nxt["engine"] == "stub"
    m = orch.handle("metrics", {})[1]["modules"]
    assert m["crm"]["processed"] >= 5 and m["tts"]["processed"] >= 5 and m["sms"]["processed"] == 1 and m["crm"]["errors"] == 0


def test_speech_booking_with_remote_nlu_and_asr(remote_stack):
    orch, reg, gw, operator = remote_stack
    orch.handle("call.start", {"call_id": "r2", "flow": "inbound", "caller_phone": "79035551234"})
    s = drive(orch, "r2", [
        ("inbound.welcome", ("timeout",)),  # 60 s without keypress → ASR
        ("inbound.listen_free", ("audio", "запишите меня к хирургу в пятницу после обеда")),
        ("inbound.id_self_or_other", ("dtmf", "1")), ("inbound.id_birth_check", ("digits", "12051980")),
        ("inbound.book_offer", ("speech", "да, подходит")),
    ])
    assert s["status"] == "booked" and s["errors"] == [], s
    b = reg.booking(s["vars"]["booking_id"])
    assert b.specialty == "хирург" and b.starts_at.date() == date(2026, 10, 16) and b.starts_at.hour >= 13


def test_human_only_speech_creates_operator_ticket_remotely(remote_stack):
    orch, reg, gw, operator = remote_stack
    orch.handle("call.start", {"call_id": "r3", "flow": "inbound", "caller_phone": "79165550000"})
    s = drive(orch, "r3", [("inbound.welcome", ("timeout",)), ("inbound.listen_free", ("audio", "хочу пожаловаться на врача"))])
    assert s["status"] == "transfer" and s["transfer"]["reason"] == "human_only"
    assert wait_until(lambda: len(operator.tickets) >= 1)  # the ticket is built by the remote operator service
    assert len(operator.tickets) == 1 and operator.tickets[0].reason == "human_only"
    assert reg.execute("SELECT count(*) AS n FROM operator_requests")[0]["n"] == 1

"""Parallel live calls through the threaded orchestrator: every call gets its own prompts, SMS and CRM writes."""
import threading
import time

from calls.orchestrator.app import Orchestrator
from calls.scenario.engine import Event
from tests.conftest import TODAY, wait_until


def wait_idle(r, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with r._lock:
            if r.session.finished or (r.session._pending is None and r.session._awaiting_input):
                return
        time.sleep(0.002)
    raise TimeoutError(r.session.state_name)


def drive(r, inputs):
    for inp in inputs:
        wait_idle(r)
        if isinstance(inp, tuple):
            if inp[0] == "say":
                r.on_speech(inp[1])
            elif inp[0] == "timeout":
                r._inject(Event("timeout"))
        else:
            for ch in inp:
                r.on_dtmf(ch)
    wait_idle(r)


def test_parallel_calls_do_not_interfere(flows, reg):
    orch = Orchestrator(flows, reg, today=lambda: TODAY)
    try:
        scripts = {
            "c-ivan": ("79035551234", ["1", "1", "12051980", "1", "1"], "booked", 1),
            "c-anna": ("79035551234", ["1", "2", "2", "1002#", "2", "1"], "booked", 2),
            "c-maria": ("79165550000", ["1", "1", "23111992", "3", "1"], "booked", 3),
            "c-pain": ("79165550000", [("timeout",), ("say", "у меня острая боль")], "transfer", None),
            "c-info": ("70000000000", ["3", "1", "0"], "done", None),
        }
        runners = {cid: orch.start_call(cid, "inbound", phone) for cid, (phone, *_rest) in scripts.items()}
        threads = [threading.Thread(target=drive, args=(runners[cid], scripts[cid][1])) for cid in scripts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert wait_until(lambda: len(orch.stack.sms_gateway.sent) >= 3 and len(orch.stack.operator.tickets) >= 1)  # fire-and-forget SMS / operator
        for cid, (phone, _inputs, status, patient) in scripts.items():
            r = runners[cid]
            assert r.record.status == status, (cid, r.session.state_name, r.record.errors)
            if patient:
                b = reg.booking(r.session.vars["booking_id"])
                assert b.patient_id == patient and b.status == "booked"
                assert r.record.sms and r.record.sms[0]["vars"]["patient_name"] == reg.patient_by_id(patient).short_name
        sent = orch.stack.sms_gateway.sent
        assert len(sent) == 3 and {p for p, _ in sent} == {"79035551234", "79165550000"}
        assert all(any(name in txt for name in ("Иван Иванович", "Анна Ивановна", "Мария Сергеевна")) for _, txt in sent)
        assert len(orch.stack.operator.tickets) == 1 and orch.stack.operator.tickets[0].reason == "human_only"
        m = orch.handle("metrics", {})[1]
        assert m["active_calls"] == 0 and m["modules"]["crm"]["errors"] == 0 and m["modules"]["tts"]["processed"] > 10
        assert m["tts_cache"]["hits"] > 0  # the welcome prompt is synthesised once and reused
        st = orch.handle("call.state", {"call_id": "c-ivan"})[1]
        assert st["status"] == "booked" and "snils" not in st["vars"] and "caller_phone" not in st["vars"]
    finally:
        orch.shutdown()


def test_timer_fires_timeout_when_caller_is_silent(flows, reg):
    orch = Orchestrator(flows, reg, today=lambda: TODAY)
    try:
        r = orch.start_call("silent", "reminder", "79035551234", vars={"patient_id": 1, "patient_name": "Иван Иванович", "booking_id": 1, "specialty": "терапевт",
                                                                      "specialty_dative": "терапевту", "doctor": "Иванова", "cabinet": "101", "slot_date": "2026-10-15",
                                                                      "slot_time": "08:00", "slot_date_spoken": "завтра", "slot_time_spoken": "восемь"})
        wait_idle(r)
        r.speech_chars_per_s = 10_000  # shrink the prompt-length estimate
        r._cancel_timer()
        r._arm_timer(0.05)
        assert wait_until(lambda: r.session.retries.get("start") == 1)
        assert not r.finished
        r._arm_timer(0.05)
        assert wait_until(lambda: r.finished)
        assert r.record.status == "no_answer"
    finally:
        orch.shutdown()


def test_playback_queue_and_audio_passthrough(flows, reg):
    """Bridge pull-model: prompts are queued per call, call.audio goes through the ASR module back into the dialogue."""
    from calls.orchestrator.app import Orchestrator

    orch = Orchestrator(flows, reg, today=lambda: TODAY)
    try:
        st, p = orch.handle("call.start", {"call_id": "c1", "flow": "inbound", "caller_phone": "79035551234"})
        assert st == "ok"
        deadline = time.time() + 3
        clips = []
        while time.time() < deadline and not clips:
            nxt = orch.handle("call.next_prompt", {"call_id": "c1"})[1]
            if nxt:
                clips.append(nxt)
            else:
                time.sleep(0.02)
        assert clips and clips[0]["prompt_id"] >= 1 and "pcm_b64" in clips[0] and "pcm" not in clips[0]
        orch.handle("call.prompt_done", {"call_id": "c1", "prompt_id": clips[0]["prompt_id"]})
        # DTMF phase: audio is ignored (not even sent to the ASR module)
        st, _ = orch.handle("call.audio", {"call_id": "c1", "pcm_b64": "", "rate": 8000, "text": "у меня острая боль"})
        assert st == "ok"
        time.sleep(0.3)
        assert not any(m.module == "asr" for m in orch.get("c1").record.module_calls)
        # 60 s without keypress → listen_free; now the stub ASR echoes the text and it must reach the dialogue as speech
        orch.get("c1")._inject(Event("timeout"))
        assert orch.get("c1").listening
        st, _ = orch.handle("call.audio", {"call_id": "c1", "pcm_b64": "", "rate": 8000, "text": "у меня острая боль"})
        deadline = time.time() + 3
        while time.time() < deadline and not orch.handle("call.state", {"call_id": "c1"})[1]["finished"]:
            time.sleep(0.02)
        rec = orch.handle("call.state", {"call_id": "c1"})[1]
        assert rec["finished"] and rec["transfer"]["reason"] == "human_only" and rec["status"] == "transfer"
        assert orch.handle("call.next_prompt", {"call_id": "zzz"})[0] == "error"
    finally:
        orch.shutdown()

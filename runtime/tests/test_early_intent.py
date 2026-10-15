"""Early intent + caller state on streaming partials (own design, see calls/nlu/early.py) and its orchestrator wiring."""

from __future__ import annotations

import base64
import time

import numpy as np
import pytest

from calls.nlu.early import STATES, EarlyIntentTracker, ProsodyFeatures, ProsodyTracker, estimate_user_state
from calls.orchestrator.app import Orchestrator
from calls.scenario.engine import Event
from tests.conftest import TODAY, wait_until


def run(partials, final=None, **kw):
    t = EarlyIntentTracker(**kw)
    for at, text in partials:
        d = t.on_partial(at, text)
        if d is not None:
            return d, t
    if final is not None:
        return t.on_final(*final), t
    return t.decision, t


BOOK = [(0, "хочу"), (250, "хочу запи"), (500, "хочу записаться"), (750, "хочу записаться к"), (1000, "хочу записаться к терапевту")]


def test_booking_is_decided_from_partials_before_the_sentence_ends():
    d, t = run(BOOK, final=(1500, "хочу записаться к терапевту на пятницу"))
    assert d.intent == "book" and d.reason == "evidence" and not d.escalate
    assert d.decided_at_ms <= 1000 and d.confidence >= 0.9
    assert t.restarts() == 0.0  # "запи" → "записаться" is word growth, not a retraction
    assert d.user_state.label == "calm"


@pytest.mark.parametrize(
    ("partials", "intent", "state"),
    [
        ([(0, "я хочу"), (300, "я хочу пожаловаться"), (600, "я хочу пожаловаться на хамство")], "complaint", "agitated"),
        ([(0, "у меня"), (300, "у меня очень"), (600, "у меня очень сильно болит"), (900, "у меня очень сильно болит живот")], "emergency", "distressed"),
        ([(0, "дайте"), (300, "дайте оператора"), (600, "дайте оператора пожалуйста")], "operator", "calm"),
    ],
)
def test_human_only_intents_escalate_immediately(partials, intent, state):
    d, _ = run(partials)
    assert d is not None and d.escalate and d.reason == "escalate"
    assert d.intent == intent and d.decided_at_ms <= 600 and d.confidence >= 0.9
    assert d.user_state.label == state


def test_vague_speech_waits_for_the_deadline_and_is_confused():
    partials = [(0, "ну"), (400, "ну это"), (900, "ну это самое"), (1500, "ну это самое я не понимаю"), (2900, "ну это самое я не понимаю куда")]
    d, t = run(partials)
    assert d is None  # nothing conclusive, deadline not reached
    d = t.tick(3050)  # timer without new text → forced decision at the deadline
    assert d is not None and d.reason == "deadline" and d.intent == "unknown" and d.confidence == 0.0
    assert d.user_state.label == "confused" and any(c.startswith("confusion") for c in d.user_state.cues)


def test_hysteresis_and_margin_against_flicker():
    # one strong keyword needs two consecutive agreeing partials; the lead over the runner-up must be ≥ margin
    t = EarlyIntentTracker()
    assert t.on_partial(0, "отмените") is None
    d = t.on_partial(300, "отмените мою")
    assert d is not None and d.intent == "cancel" and d.reason == "evidence"
    t2 = EarlyIntentTracker()
    assert t2.on_partial(0, "перенести или отменить") is None  # reschedule 3.0 vs cancel 3.0 → no lead
    assert t2.on_partial(300, "перенести или отменить запись") is None
    d2 = t2.on_final(600, "перенести или отменить запись")
    assert d2.reason == "final" and d2.intent in ("reschedule", "cancel") and d2.confidence < 0.8


def test_final_always_decides_and_tracker_is_single_shot():
    t = EarlyIntentTracker()
    d = t.on_final(900, "скажите до скольки вы работаете в субботу")
    assert d.intent == "info_schedule" and d.reason == "final"
    assert t.on_partial(1200, "что-то ещё") is d  # decided once per utterance


def test_prosody_features_from_synthetic_voice():
    sr = 8000

    def tone(f0, sec, amp):
        tt = np.arange(int(sr * sec)) / sr
        return (amp * 0.5 * np.sign(np.sin(2 * np.pi * f0 * tt)) + amp * 0.5 * np.sin(2 * np.pi * f0 * tt)).astype(np.float32)

    calm = ProsodyTracker()
    calm.feed(np.concatenate([tone(120, 0.6, 0.1), np.zeros(800, dtype=np.float32), tone(125, 0.6, 0.1)]))
    f = calm.features()
    assert 100 <= f.f0_hz <= 140 and f.f0_var < 0.15 and abs(f.loudness_delta_db) < 2 and f.speech_ms >= 1000
    loud = ProsodyTracker()
    loud.feed(np.concatenate([tone(120, 0.5, 0.05), tone(200, 0.5, 0.3), tone(260, 0.5, 0.45)]))  # louder and higher
    g = loud.features()
    assert g.loudness_delta_db > 8 and g.f0_var > 0.3
    # int16 bytes are accepted too
    pcm = (tone(150, 0.3, 0.2) * 32767).astype(np.int16).tobytes()
    b = ProsodyTracker()
    b.feed(pcm)
    assert b.features().speech_ms >= 280


def test_user_state_scoring():
    assert set(STATES) == {"calm", "hurried", "agitated", "distressed", "confused"}
    calm = estimate_user_state("хочу записаться к терапевту", ProsodyFeatures(speech_ms=1500), 2.5, 0.0)
    assert calm.label == "calm"
    hurried = estimate_user_state("мне срочно нужно побыстрее записаться", ProsodyFeatures(speech_ms=1500), 4.5, 0.0)
    assert hurried.label == "hurried"
    angry = estimate_user_state("сколько можно никто не берёт трубку безобразие", ProsodyFeatures(speech_ms=1500, loudness_delta_db=10, loudness_var_db=9, f0_var=0.5), 3.0, 0.0)
    assert angry.label == "agitated" and any(c.startswith("anger") for c in angry.cues)
    pain = estimate_user_state("мне плохо очень болит сердце", ProsodyFeatures(speech_ms=1500, pause_ratio=0.5, loudness_delta_db=-6), 1.2, 0.0)
    assert pain.label == "distressed"
    confused = estimate_user_state("алло это поликлиника я не понимаю куда я попал", ProsodyFeatures(speech_ms=1500, pause_ratio=0.5), 1.3, 0.4)
    assert confused.label == "confused"


# ---- orchestrator wiring --------------------------------------------------------------------------------------------


def _wait(orch, call_id, pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = orch.handle("call.state", {"call_id": call_id})[1]
        if pred(st):
            return st
        time.sleep(0.02)
    raise AssertionError(orch.handle("call.state", {"call_id": call_id})[1])


def test_partials_escalate_before_the_final_transcript_and_state_reaches_the_ticket(flows, reg):
    orch = Orchestrator(flows, reg, today=lambda: TODAY)
    try:
        orch.handle("call.start", {"call_id": "e1", "flow": "inbound", "caller_phone": "79165550000"})
        # DTMF phase: partials are ignored (speech recognition is not armed yet)
        assert orch.handle("call.partial", {"call_id": "e1", "text": "у меня очень сильно болит", "at_ms": 0})[1]["early"] is None
        r = orch.get("e1")
        r._inject(Event("timeout"))  # 60 s without keypress → listen_free
        assert r.listening
        pcm = (0.3 * np.sin(2 * np.pi * 180 * np.arange(8000) / 8000) * 32767).astype(np.int16).tobytes()
        orch.handle("call.audio", {"call_id": "e1", "pcm_b64": base64.b64encode(pcm).decode(), "rate": 8000, "text": ""})
        orch.handle("call.partial", {"call_id": "e1", "text": "у меня", "at_ms": 100})
        orch.handle("call.partial", {"call_id": "e1", "text": "у меня очень", "at_ms": 400})
        st = orch.handle("call.partial", {"call_id": "e1", "text": "у меня очень сильно болит", "at_ms": 700})[1]
        assert st["early"] and st["early"]["escalate"] and st["early"]["intent"] == "emergency" and st["early"]["decided_at_ms"] == 600.0
        assert st["user_state"] == "distressed"
        st = _wait(orch, "e1", lambda s: s["finished"])
        assert st["status"] == "transfer" and st["transfer"]["reason"] == "human_only"
        # the final transcript of the same utterance must not be processed twice
        orch.handle("call.speech", {"call_id": "e1", "text": "у меня очень сильно болит живот"})
        tickets = orch.stack.operator.tickets
        assert wait_until(lambda: len(tickets) >= 1)
        assert len(tickets) == 1
        t = tickets[0]
        assert t.user_state["label"] == "distressed" and t.user_state["early_intent"] == "emergency"
        assert any("плохо себя чувствует" in n for n in t.notes)
        assert "Состояние звонящего" in t.to_html() and "болит" in t.to_json()
        assert r.record.utterances == ["у меня очень сильно болит"]
    finally:
        orch.shutdown()


def test_non_escalating_early_decision_is_only_a_hint(flows, reg):
    orch = Orchestrator(flows, reg, today=lambda: TODAY)
    try:
        orch.handle("call.start", {"call_id": "e2", "flow": "inbound", "caller_phone": "79035551234"})
        r = orch.get("e2")
        r._inject(Event("timeout"))
        for at, text in BOOK:
            st = orch.handle("call.partial", {"call_id": "e2", "text": text, "at_ms": at})[1]
        assert st["early"]["intent"] == "book" and not st["early"]["escalate"]
        assert st["state"] == "inbound.listen_free" and not st["finished"]  # nothing injected: the dialogue waits for the final
        orch.handle("call.speech", {"call_id": "e2", "text": "хочу записаться к терапевту на пятницу"})
        st = _wait(orch, "e2", lambda s: s["state"] != "inbound.listen_free")
        assert r.record.utterances == ["хочу записаться к терапевту на пятницу"]
        assert st["user_state"] == "calm" and len(r.record.early) == 1
    finally:
        orch.shutdown()


def test_asr_event_adapter_uses_engine_stability():
    from calls.asr.events import AsrEvent, Word

    t = EarlyIntentTracker()
    w = [Word(0.0, 0.3, "отмените", True), Word(0.3, 0.5, "мою", False)]
    assert t.on_asr_event(AsrEvent("partial", 0.5, "отмените мою", tuple(w))) is None
    w2 = [Word(0.0, 0.3, "отмените", True), Word(0.3, 0.5, "мою", True), Word(0.5, 0.8, "запись", False)]
    d = t.on_asr_event(AsrEvent("partial", 0.8, "отмените мою запись", tuple(w2)))
    assert d is not None and d.intent == "cancel" and d.decided_at_ms == 300.0
    t2 = EarlyIntentTracker()
    d2 = t2.on_asr_event(AsrEvent("final", 1.2, "режим работы в праздники", endpoint=1.0))
    assert d2.reason == "final" and d2.intent == "info_holidays"

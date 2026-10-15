"""Deeray GigaAM v3 Runtime stream protocol → normalised partial/stable/final events (ARCHITECTURE §S5)."""

import pytest

from calls.asr import AsrEvent, DeerayStreamTracker, Word


def w(start, end, text, stable=False):
    return {"start": start, "end": end, "text": text, "stable": stable}


def test_words_revise_from_replaces_tail():
    t = DeerayStreamTracker()
    ev = t.on_message({"type": "words", "at": 1.0, "revise_from": 0, "words": [w(0.1, 0.4, "добрый"), w(0.5, 0.8, "ден")]})
    assert [e.kind for e in ev] == ["partial"] and ev[0].text == "добрый ден"
    ev = t.on_message({"type": "words", "at": 1.2, "revise_from": 1, "words": [w(0.5, 0.9, "день"), w(1.0, 1.1, "я")]})
    assert ev[0].text == "добрый день я"
    assert t.open_text == "добрый день я"
    assert t.violations == []


def test_stable_then_final_with_endpoint_latency():
    t = DeerayStreamTracker()
    t.on_message({"type": "words", "at": 1.0, "revise_from": 0, "words": [w(0.1, 0.4, "хочу"), w(0.5, 0.9, "оплатить"), w(1.0, 1.3, "счёт")]})
    ev = t.on_message({"type": "stable", "at": 1.4, "upto": 2})
    assert ev[0].kind == "stable" and ev[0].text == "хочу оплатить"
    assert t.stable_text == "хочу оплатить"
    ev = t.on_message({"type": "final", "at": 1.9, "upto": 3, "endpoint": 1.5})
    f = ev[0]
    assert f.kind == "final" and f.text == "хочу оплатить счёт" and f.endpoint == 1.5
    assert f.latency == pytest.approx(0.4)
    assert t.committed_text == "хочу оплатить счёт" and t.open_text == ""
    # next utterance starts fresh at index 3
    ev = t.on_message({"type": "words", "at": 2.5, "revise_from": 3, "words": [w(2.0, 2.3, "да")]})
    assert ev[0].text == "да"


def test_stable_frontier_moving_backwards_is_a_violation_but_tolerated():
    t = DeerayStreamTracker()
    t.on_message({"type": "words", "at": 1.0, "revise_from": 0, "words": [w(0, 0.2, "a"), w(0.3, 0.5, "b"), w(0.6, 0.8, "c")]})
    t.on_message({"type": "stable", "at": 1.1, "upto": 2})
    ev = t.on_message({"type": "stable", "at": 1.2, "upto": 1})
    assert ev[0].text == "a b"  # clamped, not regressed
    assert [v.kind for v in t.violations] == ["stable"]


def test_revision_below_final_frontier_is_dropped_and_flagged():
    t = DeerayStreamTracker()
    t.on_message({"type": "words", "at": 1.0, "revise_from": 0, "words": [w(0, 0.2, "a"), w(0.3, 0.5, "b")]})
    t.on_message({"type": "final", "at": 1.1, "upto": 2, "endpoint": 0.6})
    ev = t.on_message({"type": "words", "at": 1.5, "revise_from": 0, "words": [w(0, 0.2, "x"), w(0.3, 0.5, "y"), w(0.7, 0.9, "z")]})
    assert ev[0].text == "z"
    assert t.committed_text == "a b"
    assert [v.kind for v in t.violations] == ["final"]


def test_changing_stable_text_is_flagged():
    t = DeerayStreamTracker()
    t.on_message({"type": "words", "at": 1.0, "revise_from": 0, "words": [w(0, 0.2, "a"), w(0.3, 0.5, "b")]})
    t.on_message({"type": "stable", "at": 1.1, "upto": 1})
    t.on_message({"type": "words", "at": 1.2, "revise_from": 0, "words": [w(0, 0.2, "A"), w(0.3, 0.5, "b")]})
    assert [v.kind for v in t.violations] == ["stable"]


def test_end_flushes_uncommitted_words_as_final():
    t = DeerayStreamTracker()
    t.on_message({"type": "words", "at": 1.0, "revise_from": 0, "words": [w(0, 0.2, "спасибо")]})
    ev = t.on_message({"type": "end", "at": 1.3})
    assert [e.kind for e in ev] == ["final", "end"]
    assert ev[0].flushed and ev[0].text == "спасибо" and ev[0].latency is None
    assert t.closed


def test_upto_as_time_is_converted_to_word_index():
    t = DeerayStreamTracker()
    t.on_message({"type": "words", "at": 1.0, "revise_from": 0, "words": [w(0.1, 0.4, "a"), w(0.5, 0.9, "b"), w(1.0, 1.3, "c")]})
    ev = t.on_message({"type": "stable", "at": 1.4, "upto": 0.95})
    assert ev[0].text == "a b"


def test_error_and_unknown_messages():
    t = DeerayStreamTracker()
    ev = t.on_message({"type": "error", "at": 0.5, "message": "model busy"})
    assert ev == [AsrEvent("error", 0.5, message="model busy")]
    assert t.on_message({"type": "bogus"}) == []
    assert t.violations[-1].kind == "unknown"


def test_time_going_backwards_is_flagged():
    t = DeerayStreamTracker()
    t.on_message({"type": "words", "at": 2.0, "revise_from": 0, "words": [w(0, 0.2, "a")]})
    t.on_message({"type": "words", "at": 1.5, "revise_from": 1, "words": [w(0.3, 0.5, "b")]})
    assert [v.kind for v in t.violations] == ["time"]
    assert t.words == [Word(0, 0.2, "a"), Word(0.3, 0.5, "b")]

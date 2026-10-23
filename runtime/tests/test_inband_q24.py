"""In-band detector vs the ITU-T Q.24 acceptance matrix (docs/ARCHITECTURE.md §S3.2).

Fast (CI): one-factor-at-a-time around the nominal case, every digit × codec at the Q.24
minimum duration, plus a seeded random subset of the factorial.
Full factorial (72 000 cases): ``pytest -m full``.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from calls.audio.level import dbm0_to_amplitude
from calls.dtmf import DigitStart, InbandDtmfDetector
from calls.dtmf.matrix import (
    ALL_DIGITS,
    Case,
    full_factorial,
    full_factorial_size,
    one_factor_at_a_time,
    random_subset,
    run_case,
)
from calls.dtmf.synth import dtmf_sequence, dtmf_tone, speech_like


def _check(cases):
    failures = [f"{o.case.label()} expected={o.case.expected} got={o.digits!r}" for o in map(run_case, cases) if not o.passed]
    assert not failures, "\n".join(failures[:40]) + (f"\n... {len(failures)} failures" if len(failures) > 40 else "")


def test_one_factor_at_a_time():
    cases = one_factor_at_a_time()
    assert len(cases) > 30
    _check(cases)


@pytest.mark.parametrize("codec", ["lin", "alaw", "ulaw"])
def test_every_digit_at_q24_minimum_duration(codec):
    _check([Case(digit=d, duration_ms=40.0, codec=codec, level_dbm0=-25.0, twist_db=-8.0) for d in ALL_DIGITS])
    _check([Case(digit=d, duration_ms=40.0, codec=codec, level_dbm0=-3.0, twist_db=4.0) for d in ALL_DIGITS])


def test_q24_corner_combinations_detect():
    """Worst-case operate corners: minimum level, extreme twist, extreme deviation, noise."""
    cases = [
        Case(digit=d, duration_ms=40.0, freq_dev=dev, twist_db=tw, level_dbm0=-25.0, codec=codec, snr_db=12.0)
        for d in ("1", "9", "D", "*")
        for dev in (0.015, -0.015)
        for tw in (-8.0, 4.0)
        for codec in ("alaw", "ulaw")
    ]
    _check(cases)


def test_q24_corner_combinations_reject():
    cases = [
        Case(digit=d, duration_ms=23.0, level_dbm0=-3.0, codec=codec) for d in ALL_DIGITS for codec in ("lin", "ulaw")
    ] + [
        Case(digit=d, duration_ms=300.0, freq_dev=dev, level_dbm0=-3.0) for d in ALL_DIGITS for dev in (0.035, -0.035)
    ] + [Case(digit=d, duration_ms=300.0, level_dbm0=-55.0) for d in ALL_DIGITS]
    _check(cases)


def test_random_subset_of_factorial():
    _check(random_subset(400))


@pytest.mark.full
def test_full_factorial():
    size = full_factorial_size()
    assert size == 16 * 5 * 5 * 5 * 4 * 3 * 3
    failures = []
    for case in full_factorial():
        o = run_case(case)
        if not o.passed:
            failures.append(f"{case.label()} expected={case.expected} got={o.digits!r}")
    assert not failures, "\n".join(failures[:100]) + f"\n... {len(failures)} / {size} failures"


# --------------------------------------------------------------------------- structure


def test_pause_of_40ms_separates_identical_digits():
    """Q.24: a ≥ 40 ms pause must yield two digits, even for the same key."""
    for gap in (40.0, 60.0):
        pcm, spans = dtmf_sequence("11", on_ms=50.0, off_ms=gap)
        det = InbandDtmfDetector()
        ev = det.push(pcm) + det.finish()
        assert "".join(e.digit for e in ev if isinstance(e, DigitStart)) == "11", gap


def test_short_interruption_is_bridged():
    """ETSI ES 201 235-3: drop-outs < 20 ms inside a tone must not split it."""
    pcm, spans = dtmf_sequence("77", on_ms=50.0, off_ms=10.0)
    det = InbandDtmfDetector()
    ev = det.push(pcm) + det.finish()
    assert "".join(e.digit for e in ev if isinstance(e, DigitStart)) == "7"


def test_dialled_string_all_digits_every_codec():
    for codec in ("lin", "alaw", "ulaw"):
        pcm, spans = dtmf_sequence("1234567890*#ABCD", on_ms=45.0, off_ms=45.0, codec=codec, level_dbm0=-20.0)
        det = InbandDtmfDetector()
        ev = det.push(pcm) + det.finish()
        assert "".join(e.digit for e in ev if isinstance(e, DigitStart)) == "1234567890*#ABCD", codec


def test_measurements_reported_on_digit_start():
    o = run_case(Case(digit="D", freq_dev=0.015, twist_db=-6.0, level_dbm0=-20.0))
    assert o.digits == "D"
    s = o.starts[0]
    assert s.freq_dev_low == pytest.approx(0.015, abs=0.002)
    assert s.freq_dev_high == pytest.approx(0.015, abs=0.002)
    assert s.twist_db == pytest.approx(-6.0, abs=0.5)
    assert s.level_low_dbm0 == pytest.approx(-20.0, abs=0.5)
    assert s.level_high_dbm0 == pytest.approx(-26.0, abs=0.5)
    assert s.detector_version == det_version()


def det_version() -> str:
    return InbandDtmfDetector().p.version


# --------------------------------------------------------------------------- non-DTMF signals


def _single_tone(freq: float, ms: float, dbm0: float = -10.0) -> np.ndarray:
    n = int(ms * 8)
    return dbm0_to_amplitude(dbm0) * np.cos(2 * math.pi * freq * np.arange(n) / 8000.0)


@pytest.mark.parametrize("freq", [425.0, 697.0, 1000.0, 1336.0, 1633.0])
def test_single_frequency_tones_are_not_digits(freq):
    det = InbandDtmfDetector()
    ev = det.push(_single_tone(freq, 1000.0)) + det.finish()
    assert ev == []


def test_three_tone_signal_is_rejected_by_dominance():
    x = dtmf_tone("1", 200.0, level_dbm0=-10.0) + _single_tone(1336.0, 200.0, -10.0)
    det = InbandDtmfDetector()
    assert det.push(x) + det.finish() == []


def test_two_low_group_tones_are_rejected():
    x = _single_tone(697.0, 200.0) + _single_tone(941.0, 200.0)
    det = InbandDtmfDetector()
    assert det.push(x) + det.finish() == []


def test_silence_and_noise_produce_nothing():
    det = InbandDtmfDetector()
    assert det.push(np.zeros(16000)) == []
    rng = np.random.default_rng(0)
    assert det.push(rng.normal(0, 2000, 80000)) + det.finish() == []


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_speech_surrogate_talk_off_smoke(seed):
    """Not a talk-off qualification (that needs a speech corpus, see ARCHITECTURE §S3.2) —
    a smoke test that harmonic voiced signals with pitch glides do not register."""
    det = InbandDtmfDetector()
    ev = det.push(speech_like(10000.0, seed=seed)) + det.finish()
    assert [e.digit for e in ev if isinstance(e, DigitStart)] == []


# --------------------------------------------------------------------------- context robustness


def _digits(x: np.ndarray, codec: str = "ulaw") -> str:
    from calls.dtmf.synth import apply_codec

    det = InbandDtmfDetector()
    ev = det.push(apply_codec(x, codec)) + det.finish()
    return "".join(e.digit for e in ev if isinstance(e, DigitStart))


def test_quiet_digit_after_loud_non_dtmf_burst():
    """The burst search must stop at the gap: a louder, rejected burst shortly before a
    valid digit must not raise the −6 dB threshold of the digit."""
    gap = np.zeros(480)  # 60 ms
    x = np.concatenate([speech_like(1000.0, seed=4, level_dbm0=-6.0), gap, dtmf_tone("5", 50.0, level_dbm0=-20.0), np.zeros(800)])
    assert _digits(x) == "5"
    x = np.concatenate([np.zeros(800), dtmf_tone("8", 300.0, level_dbm0=-3.0, freq_dev=0.035), np.zeros(320), dtmf_tone("8", 45.0, level_dbm0=-23.0), np.zeros(800)])
    assert _digits(x) == "8"


def test_long_key_hold_then_next_digit():
    x = np.concatenate([np.zeros(800), dtmf_tone("1", 2000.0), np.zeros(400), dtmf_tone("2", 60.0), np.zeros(800)])
    assert _digits(x) == "12"


def test_level_jumps_between_digits():
    parts = [np.zeros(800)]
    for d, lv in zip("3691", (-3.0, -30.0, -3.0, -30.0)):
        parts += [dtmf_tone(d, 50.0, level_dbm0=lv), np.zeros(400)]
    assert _digits(np.concatenate(parts)) == "3691"

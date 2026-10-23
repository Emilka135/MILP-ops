"""Timing and determinism criteria of the in-band detector (docs/ARCHITECTURE.md §S3.2).

* DigitStart ≤ 60 ms after tone onset; DigitEnd ≤ 40 ms after tone end (latency budget I-2).
* |t_start − onset| and |t_end − end| ≤ 5 ms (media-clock accuracy).
* Identical events regardless of how the PCM is chunked and across repeated runs.
"""

from __future__ import annotations

import hashlib
import pickle

import pytest

from calls.dtmf import DigitEnd, DigitStart, DtmfParams, InbandDtmfDetector
from calls.dtmf.matrix import ALL_DIGITS
from calls.dtmf.synth import dtmf_case, dtmf_sequence

FS = 8000
MS = FS // 1000


def _events(pcm, chunk=None):
    det = InbandDtmfDetector()
    if chunk is None:
        ev = det.push(pcm)
    else:
        ev = []
        for i in range(0, len(pcm), chunk):
            ev += det.push(pcm[i : i + chunk])
    return ev + det.finish()


@pytest.mark.parametrize("codec", ["lin", "ulaw"])
@pytest.mark.parametrize("duration_ms", [40.0, 60.0, 100.0, 300.0])
def test_latency_and_timestamp_accuracy(codec, duration_ms):
    worst_start_lat = worst_end_lat = worst_t0 = worst_t1 = 0
    for digit in ALL_DIGITS:
        for shift in range(0, 20, 4):  # alignment of the onset relative to the hop grid
            pcm, on, off = dtmf_case(digit, duration_ms, codec=codec, lead_ms=100.0 + shift / MS, level_dbm0=-12.0)
            ev = _events(pcm)
            starts = [e for e in ev if isinstance(e, DigitStart)]
            ends = [e for e in ev if isinstance(e, DigitEnd)]
            assert [e.digit for e in starts] == [digit], (digit, shift)
            assert [e.digit for e in ends] == [digit], (digit, shift)
            s, e = starts[0], ends[0]
            worst_start_lat = max(worst_start_lat, s.t_detect - on)
            worst_end_lat = max(worst_end_lat, e.t_detect - off)
            worst_t0 = max(worst_t0, abs(s.t_start - on))
            worst_t1 = max(worst_t1, abs(e.t_end - off))
            assert e.t_start == s.t_start
    assert worst_start_lat <= 60 * MS, f"DigitStart latency {worst_start_lat / MS:.1f} ms"
    assert worst_end_lat <= 40 * MS, f"DigitEnd latency {worst_end_lat / MS:.1f} ms"
    assert worst_t0 <= 5 * MS, f"t_start error {worst_t0 / MS:.1f} ms"
    assert worst_t1 <= 5 * MS, f"t_end error {worst_t1 / MS:.1f} ms"


def test_duration_threshold_is_monotone_and_alignment_independent():
    """Below the Q.24 non-operate limit nothing is ever confirmed; from the operate limit
    on everything is, for every alignment; in between the decision is monotone in D."""
    decided = {}
    for duration_ms in (10.0, 20.0, 23.0, 25.0, 28.0, 30.0, 32.0, 34.0, 36.0, 40.0):
        hits = []
        for shift in range(0, 20, 2):
            pcm, on, off = dtmf_case("8", duration_ms, lead_ms=100.0 + shift / MS)
            hits.append(any(isinstance(e, DigitStart) for e in _events(pcm)))
        decided[duration_ms] = hits
    for d in (10.0, 20.0, 23.0, 25.0, 28.0):
        assert not any(decided[d]), d
    for d in (34.0, 36.0, 40.0):
        assert all(decided[d]), d
    rates = [sum(decided[d]) for d in sorted(decided)]
    assert rates == sorted(rates), rates


def test_events_are_identical_for_any_chunking():
    pcm, _ = dtmf_sequence("1470*#AD", on_ms=45.0, off_ms=40.0, codec="alaw", level_dbm0=-18.0)
    ref = _events(pcm, 160)  # 20 ms frames
    assert "".join(e.digit for e in ref if isinstance(e, DigitStart)) == "1470*#AD"
    for chunk in (1, 7, 80, 205, 800, 1000, len(pcm)):
        assert _events(pcm, chunk) == ref, chunk


def test_repeated_runs_are_bitwise_identical():
    pcm, _, _ = dtmf_case("3", 80.0, snr_db=15.0, codec="ulaw", seed=5)
    digests = {hashlib.sha256(pickle.dumps(_events(pcm, 160))).hexdigest() for _ in range(5)}
    assert len(digests) == 1


def test_pcm16_bytes_input_matches_array_input():
    pcm, _, _ = dtmf_case("6", 60.0)
    as_bytes = pcm.astype("<i2").tobytes()
    assert _events(as_bytes) == _events(pcm)


def test_finish_closes_an_open_digit():
    pcm, on, off = dtmf_case("2", 100.0, trail_ms=0.0)
    det = InbandDtmfDetector()
    ev = det.push(pcm)
    assert [type(e) for e in ev] == [DigitStart]
    tail = det.finish()
    assert [type(e) for e in tail] == [DigitEnd]
    assert tail[0].t_end == det.position
    assert det.active_digit is None


def test_params_validation():
    with pytest.raises(ValueError):
        DtmfParams(min_duration_ms=20.0)  # below the window length
    with pytest.raises(ValueError):
        DtmfParams(hop=0)
    assert DtmfParams().hop_ms == pytest.approx(2.5)
    assert DtmfParams().window_ms == pytest.approx(25.625)


def test_realtime_factor_budget():
    """≤ 2 % of real time per call on the reference Python implementation."""
    import time

    pcm, _ = dtmf_sequence("1234567890" * 3, on_ms=60.0, off_ms=60.0)
    det = InbandDtmfDetector()
    t0 = time.perf_counter()
    for i in range(0, len(pcm), 160):
        det.push(pcm[i : i + 160])
    elapsed = time.perf_counter() - t0
    assert elapsed / (len(pcm) / FS) < 0.05  # generous for CI machines; typical 0.01

"""DTMF arbiter: exactly-once delivery, shadowing, dedupe, in-band fallback (ARCHITECTURE §S3.3)."""

from calls.dtmf.arbiter import DtmfArbiter

MS = 8


def acting(events):
    return [(e.digit, e.source) for e in events if not e.shadow]


def test_oob_acts_and_inband_is_shadow_when_negotiated():
    a = DtmfArbiter(oob_negotiated=True)
    ev = a.on_inband("5", 1000, 1500)
    assert len(ev) == 1 and ev[0].shadow and ev[0].source == "inband"
    ev = a.on_oob("5", 1000, 1520)
    assert acting(ev) == [("5", "oob")]
    assert a.tick(10_000) == []


def test_double_feed_yields_exactly_one_acting_event_in_any_order():
    for order in ("oob-first", "inband-first"):
        a = DtmfArbiter(oob_negotiated=True, inband_fallback=True)
        out = []
        if order == "oob-first":
            out += a.on_oob("7", 2000, 2400)
            out += a.on_inband("7", 2010, 2390)
        else:
            out += a.on_inband("7", 2010, 2390)
            out += a.on_oob("7", 2000, 2400)
        out += a.tick(2400 + 1000 * MS)
        assert acting(out) == [("7", "oob")], order
        assert [e.seq for e in out] == sorted({e.seq for e in out})


def test_inband_acts_when_telephone_event_not_negotiated():
    a = DtmfArbiter(oob_negotiated=False)
    out = a.on_inband("1", 100, 500) + a.on_inband("1", 450, 900) + a.on_inband("1", 2000, 2400)
    assert acting(out) == [("1", "inband"), ("1", "inband")]  # second overlapped → deduped


def test_inband_fallback_promotes_after_wait():
    a = DtmfArbiter(oob_negotiated=True, inband_fallback=True, fallback_wait_ms=150.0)
    shadow = a.on_inband("3", 1000, 1400)
    assert shadow[0].shadow
    assert a.tick(1400 + 149 * MS) == []
    out = a.tick(1400 + 150 * MS)
    assert acting(out) == [("3", "inband-fallback")]
    # a late OOB for the same press is a duplicate, not a second digit
    assert a.on_oob("3", 1005, 1410) == []


def test_fallback_disabled_never_promotes():
    a = DtmfArbiter(oob_negotiated=True, inband_fallback=False)
    a.on_inband("3", 1000, 1400)
    assert a.tick(1_000_000) == []


def test_dedupe_window_is_100ms_and_digit_specific():
    a = DtmfArbiter(oob_negotiated=True)
    a.on_oob("4", 1000, 1400)
    assert a.on_oob("4", 1400 + 99 * MS, 1400 + 400 * MS) == []  # within 100 ms gap → dup
    assert acting(a.on_oob("4", 1400 + 100 * MS, 1400 + 500 * MS)) == [("4", "oob")]
    assert acting(a.on_oob("5", 1000, 1400)) == [("5", "oob")]  # different digit → not a dup


def test_seq_is_monotonic_across_sources():
    a = DtmfArbiter(oob_negotiated=True, inband_fallback=True)
    seqs = [e.seq for e in a.on_inband("1", 0, 400) + a.on_oob("1", 0, 400) + a.on_oob("2", 4000, 4400)]
    assert seqs == [1, 2, 3]

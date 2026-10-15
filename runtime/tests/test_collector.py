"""collect_digits: deterministic outcomes on the media clock (ARCHITECTURE §S3.4)."""

import pytest

from calls.dtmf.collector import CollectSpec, DigitCollector

MS = 8


def test_terminator_completes_with_min_digits():
    c = DigitCollector(CollectSpec(min_digits=4, max_digits=16), t_begin=0)
    for i, d in enumerate("1234"):
        assert c.on_digit(d, 1000 + i * 2000) is None
    r = c.on_digit("#", 9000)
    assert (r.digits, r.reason, r.ok, r.t_first, r.t_last, r.t_done) == ("1234", "terminator", True, 1000, 9000, 9000)


def test_terminator_before_min_digits_is_not_ok():
    c = DigitCollector(CollectSpec(min_digits=4), t_begin=0)
    c.on_digit("1", 1000)
    r = c.on_digit("#", 2000)
    assert (r.digits, r.reason, r.ok) == ("1", "terminator", False)


def test_max_digits_completes_immediately():
    c = DigitCollector(CollectSpec(min_digits=1, max_digits=3), t_begin=0)
    assert c.on_digit("9", 100) is None
    assert c.on_digit("8", 200) is None
    r = c.on_digit("7", 300)
    assert (r.digits, r.reason, r.ok) == ("987", "complete", True)
    assert c.on_digit("6", 400) is r  # terminal state is sticky


def test_cancel_digit():
    c = DigitCollector(CollectSpec(min_digits=1, max_digits=4, cancel="*"), t_begin=0)
    c.on_digit("1", 100)
    r = c.on_digit("*", 200)
    assert (r.digits, r.reason, r.ok) == ("1", "cancel", False)


def test_first_digit_timeout_fires_exactly_on_the_media_clock():
    spec = CollectSpec(first_digit_timeout_ms=5000, inter_digit_timeout_ms=3000)
    c = DigitCollector(spec, t_begin=16_000)
    assert c.deadline() == 16_000 + 5000 * MS
    assert c.on_tick(c.deadline() - 1) is None
    r = c.on_tick(c.deadline() + 7)  # ticks arrive per 20 ms frame, result is still exact
    assert (r.digits, r.reason, r.ok, r.t_done) == ("", "timeout", False, 16_000 + 5000 * MS)


def test_inter_digit_timeout_returns_partial_string():
    spec = CollectSpec(min_digits=2, max_digits=6, inter_digit_timeout_ms=3000)
    c = DigitCollector(spec, t_begin=0)
    c.on_digit("4", 1000)
    c.on_digit("2", 2000)
    assert c.deadline() == 2000 + 3000 * MS
    r = c.on_tick(2000 + 3000 * MS)
    assert (r.digits, r.reason, r.ok, r.t_first, r.t_last) == ("42", "timeout", True, 1000, 2000)


def test_digit_arriving_after_deadline_is_a_timeout():
    c = DigitCollector(CollectSpec(first_digit_timeout_ms=1000), t_begin=0)
    r = c.on_digit("5", 1000 * MS + 1)
    assert (r.digits, r.reason) == ("", "timeout")


def test_hangup():
    c = DigitCollector(CollectSpec(min_digits=1), t_begin=0)
    c.on_digit("3", 500)
    r = c.on_hangup(900)
    assert (r.digits, r.reason, r.ok, r.t_done) == ("3", "hangup", False, 900)


def test_spec_validation():
    with pytest.raises(ValueError):
        CollectSpec(min_digits=5, max_digits=4)
    with pytest.raises(ValueError):
        CollectSpec(terminators=("#",), cancel="#")

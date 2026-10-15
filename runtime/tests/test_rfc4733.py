"""RFC 4733 telephone-event parser: exactly one digit per event, lost-end recovery."""

import pytest

from calls.dtmf.rfc4733 import OobDigit, TelephoneEventParser


def pkt(event: int, end: bool, duration: int, volume: int = 10) -> bytes:
    return bytes([event, (0x80 if end else 0) | (volume & 0x3F), (duration >> 8) & 0xFF, duration & 0xFF])


def test_parse_payload_layout():
    assert TelephoneEventParser.parse_payload(pkt(11, True, 0x01F4, 7)) == (11, True, 7, 500)
    with pytest.raises(ValueError):
        TelephoneEventParser.parse_payload(b"\x01\x02")


def test_one_digit_per_event_despite_triple_end():
    p = TelephoneEventParser()
    ts = 160_000
    out = []
    out += p.push(ts, True, pkt(5, False, 160))  # start, marker
    out += p.push(ts, False, pkt(5, False, 320))
    out += p.push(ts, False, pkt(5, False, 480))
    out += p.push(ts, False, pkt(5, True, 640))  # end ×3
    out += p.push(ts, False, pkt(5, True, 640))
    out += p.push(ts, False, pkt(5, True, 640))
    assert out == [OobDigit("5", ts, ts + 640, 10, False)]
    assert p.expire(ts + 100_000) == []


def test_event_codes_map_to_digits():
    p = TelephoneEventParser()
    digits = []
    for code in range(16):
        digits += [d.digit for d in p.push(1000 * (code + 1), True, pkt(code, True, 400))]
    assert "".join(digits) == "0123456789*#ABCD"


def test_non_dtmf_events_are_ignored():
    p = TelephoneEventParser()
    assert p.push(5000, True, pkt(32, True, 400)) == []  # e.g. fax/modem named events


def test_lost_end_packets_recovered_by_next_event():
    p = TelephoneEventParser()
    assert p.push(1000, True, pkt(1, False, 160)) == []
    assert p.push(1000, False, pkt(1, False, 480)) == []
    # end packets of '1' lost; '2' starts
    out = p.push(2000, True, pkt(2, False, 160))
    assert out == [OobDigit("1", 1000, 1480, 10, True)]
    out = p.push(2000, False, pkt(2, True, 320))
    assert out == [OobDigit("2", 2000, 2320, 10, False)]


def test_lost_end_packets_recovered_by_expiry():
    p = TelephoneEventParser(end_timeout_samples=2400)
    p.push(1000, True, pkt(9, False, 160))
    p.push(1000, False, pkt(9, False, 800))
    assert p.expire(1000 + 800 + 2399) == []
    assert p.expire(1000 + 800 + 2400) == [OobDigit("9", 1000, 1800, 10, True)]
    assert p.expire(1_000_000) == []


def test_late_update_after_end_is_ignored():
    p = TelephoneEventParser()
    assert p.push(1000, True, pkt(3, True, 640)) == [OobDigit("3", 1000, 1640, 10, False)]
    assert p.push(1000, False, pkt(3, False, 480)) == []  # reordered packet
    assert p.push(1000, False, pkt(3, True, 640)) == []


def test_duration_never_decreases_on_reordered_updates():
    p = TelephoneEventParser()
    p.push(1000, True, pkt(7, False, 480))
    p.push(1000, False, pkt(7, False, 320))  # reordered earlier update
    out = p.push(2000, True, pkt(8, True, 400))
    assert out[0] == OobDigit("7", 1000, 1480, 10, True)

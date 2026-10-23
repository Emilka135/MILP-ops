"""RFC 4733 ``telephone-event`` parser — the out-of-band DTMF source.

Payload layout (RFC 4733 §2.3):

    0                   1                   2                   3
    0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |     event     |E|R| volume    |          duration             |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

All packets of one event share the RTP timestamp of the event start; the first packet
has the marker bit set; ``duration`` grows with each update; the final packet has E=1
and is retransmitted (normally three times). The parser emits exactly one
:class:`OobDigit` per event, on the first end packet, and recovers from lost end packets
through :meth:`expire` or the start of the next event.

Timestamps are RTP clock units (8 kHz for ``telephone-event/8000``) — the same media
clock the in-band detector uses, which is what makes arbitration deterministic.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from .params import RFC4733_EVENTS


@dataclass(frozen=True)
class OobDigit:
    digit: str
    t_start: int
    t_end: int
    volume_dbm0: int  # 0..63 -> 0 .. -63 dBm0
    end_lost: bool
    source: str = "rfc4733"

    @property
    def duration_samples(self) -> int:
        return self.t_end - self.t_start


class TelephoneEventParser:
    def __init__(self, *, end_timeout_samples: int = 2400, remember: int = 64) -> None:
        """``end_timeout_samples`` (300 ms @ 8 kHz) bounds how long an event without an
        end packet stays open after its last update before it is finalised as lost."""
        self._timeout = int(end_timeout_samples)
        self._open: tuple[int, int] | None = None  # (timestamp, event)
        self._open_duration = 0
        self._open_volume = 0
        self._ended: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._remember = int(remember)

    @staticmethod
    def parse_payload(payload: bytes) -> tuple[int, bool, int, int]:
        if len(payload) < 4:
            raise ValueError("telephone-event payload shorter than 4 bytes")
        event = payload[0]
        end = bool(payload[1] & 0x80)
        volume = payload[1] & 0x3F
        duration = (payload[2] << 8) | payload[3]
        return event, end, volume, duration

    def _finish(self, key: tuple[int, int], duration: int, volume: int, end_lost: bool) -> OobDigit | None:
        if key in self._ended:
            return None
        self._ended[key] = None
        while len(self._ended) > self._remember:
            self._ended.popitem(last=False)
        ts, event = key
        if event >= len(RFC4733_EVENTS):
            return None  # non-DTMF named event (fax/modem tones etc.)
        return OobDigit(RFC4733_EVENTS[event], ts, ts + duration, volume, end_lost)

    def push(self, rtp_timestamp: int, marker: bool, payload: bytes) -> list[OobDigit]:
        """Feed one telephone-event RTP packet; returns 0..2 digits (a lost-end recovery may
        precede the current one)."""
        event, end, volume, duration = self.parse_payload(payload)
        key = (int(rtp_timestamp), int(event))
        out: list[OobDigit] = []
        if key in self._ended:
            return out  # retransmitted end packet (or late update) of a finished event
        if self._open is not None and self._open != key:
            d = self._finish(self._open, self._open_duration, self._open_volume, end_lost=True)
            if d is not None:
                out.append(d)
            self._open = None
        if end:
            d = self._finish(key, duration, volume, end_lost=False)
            if d is not None:
                out.append(d)
            self._open = None
            return out
        if self._open != key:
            self._open = key
            self._open_duration = 0
        self._open_duration = max(self._open_duration, duration)
        self._open_volume = volume
        return out

    def expire(self, now_timestamp: int) -> list[OobDigit]:
        """Finalise an open event whose updates stopped (end packets lost)."""
        if self._open is None:
            return []
        ts, _ = self._open
        if now_timestamp - (ts + self._open_duration) >= self._timeout:
            d = self._finish(self._open, self._open_duration, self._open_volume, end_lost=True)
            self._open = None
            return [d] if d is not None else []
        return []

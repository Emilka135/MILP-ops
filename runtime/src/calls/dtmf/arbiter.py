"""DTMF source arbiter (docs/ARCHITECTURE.md §S3.3).

Merges out-of-band observations (RFC 4733 / SIP INFO, delivered by the PBX) and in-band
detections into exactly-once :class:`DigitEvent`s with a monotonic ``seq``. Rules:

1. If ``telephone-event`` was negotiated on the channel, OOB is the acting source and
   in-band observations are *shadow* events (kept for QA, never acted upon) — unless
   ``inband_fallback`` is on and no OOB digit confirms an in-band one within
   ``fallback_wait``; then the in-band digit is promoted (``source="inband-fallback"``).
2. Without ``telephone-event``, in-band (and SIP INFO) observations act directly.
3. Two observations of the same digit whose intervals overlap or lie closer than
   ``dedup`` collapse into one event; OOB metadata wins.
4. Every emitted event carries a monotonic ``seq``; the caller delivers non-shadow events
   to the dialog engine exactly once.

All times are samples on the media clock; the arbiter has no notion of wall time, so a
recorded call replays to the same event sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DigitEvent:
    seq: int
    digit: str
    source: str  # "oob" | "inband" | "inband-fallback"
    t_start: int
    t_end: int
    shadow: bool = False
    detector_version: str = ""


@dataclass
class _Pending:
    digit: str
    t_start: int
    t_end: int
    deadline: int
    detector_version: str


@dataclass
class DtmfArbiter:
    oob_negotiated: bool
    inband_fallback: bool = False
    sample_rate: int = 8000
    dedup_ms: float = 100.0
    fallback_wait_ms: float = 150.0
    history_ms: float = 2000.0

    _seq: int = field(default=0, init=False)
    _accepted: list[DigitEvent] = field(default_factory=list, init=False)
    _pending: list[_Pending] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------ helpers
    def _ms(self, ms: float) -> int:
        return int(round(ms * self.sample_rate / 1000.0))

    def _is_dup(self, digit: str, t_start: int, t_end: int) -> bool:
        gap = self._ms(self.dedup_ms)
        for ev in self._accepted:
            if ev.shadow or ev.digit != digit:
                continue
            if t_start < ev.t_end + gap and ev.t_start < t_end + gap:
                return True
        return False

    def _emit(self, digit: str, source: str, t_start: int, t_end: int, *, shadow: bool, version: str = "") -> DigitEvent:
        self._seq += 1
        ev = DigitEvent(self._seq, digit, source, t_start, t_end, shadow, version)
        self._accepted.append(ev)
        horizon = max(t_end, t_start) - self._ms(self.history_ms)
        self._accepted = [e for e in self._accepted if e.t_end >= horizon]
        return ev

    # ------------------------------------------------------------------ inputs
    def on_oob(self, digit: str, t_start: int, t_end: int) -> list[DigitEvent]:
        """An out-of-band digit (RFC 4733 end packet or SIP INFO) on the media clock."""
        out: list[DigitEvent] = []
        # an OOB confirmation retires any matching pending in-band fallback
        self._pending = [
            p
            for p in self._pending
            if not (p.digit == digit and p.t_start < t_end + self._ms(self.dedup_ms) and t_start < p.t_end + self._ms(self.dedup_ms))
        ]
        if self._is_dup(digit, t_start, t_end):
            return out
        out.append(self._emit(digit, "oob", t_start, t_end, shadow=False))
        return out

    def on_inband(self, digit: str, t_start: int, t_end: int, *, detector_version: str = "") -> list[DigitEvent]:
        """A confirmed in-band digit end (:class:`~calls.dtmf.events.DigitEnd`)."""
        dup = self._is_dup(digit, t_start, t_end)
        if not self.oob_negotiated:
            if dup:
                return []
            return [self._emit(digit, "inband", t_start, t_end, shadow=False, version=detector_version)]
        if not dup and self.inband_fallback:
            self._pending.append(_Pending(digit, t_start, t_end, t_end + self._ms(self.fallback_wait_ms), detector_version))
        # shadow events are always recorded so OOB/in-band agreement can be measured
        return [self._emit(digit, "inband", t_start, t_end, shadow=True, version=detector_version)]

    def tick(self, now: int) -> list[DigitEvent]:
        """Advance the media clock; promotes in-band digits whose fallback wait expired."""
        out: list[DigitEvent] = []
        keep: list[_Pending] = []
        for p in self._pending:
            if now >= p.deadline:
                if not self._is_dup(p.digit, p.t_start, p.t_end):
                    out.append(self._emit(p.digit, "inband-fallback", p.t_start, p.t_end, shadow=False, version=p.detector_version))
            else:
                keep.append(p)
        self._pending = keep
        return out

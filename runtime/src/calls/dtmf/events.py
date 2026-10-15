"""In-band detector events. All times are sample indices on the stream's media clock."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DigitStart:
    """A digit has been confirmed (−6 dB width ≥ min_duration, all Q.24 checks passed).

    Emitted causally; intended for early barge-in. ``t_start`` is the −6 dB onset
    estimate (accurate to ~one hop), ``t_detect`` is the end of the confirming window.
    """

    digit: str
    t_start: int
    t_detect: int
    level_low_dbm0: float
    level_high_dbm0: float
    twist_db: float
    freq_dev_low: float
    freq_dev_high: float
    detector_version: str


@dataclass(frozen=True)
class DigitEnd:
    """The confirmed digit has ended (pair energy stayed below −6 dB longer than bridge_ms)."""

    digit: str
    t_start: int
    t_end: int
    t_detect: int
    detector_version: str

    @property
    def duration_samples(self) -> int:
        return self.t_end - self.t_start

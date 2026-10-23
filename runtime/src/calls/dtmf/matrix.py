"""The ITU-T Q.24 acceptance matrix for the in-band detector (docs/ARCHITECTURE.md §S3.2).

Shared by ``tests/test_inband_q24.py`` (one-factor-at-a-time + random subset in CI,
full factorial under ``-m full``) and ``tools/dtmf_matrix.py`` (pass/fail report).

Expected outcome per case follows Q.24 literally: the detector MUST detect inside the
"operate" region, MUST reject inside the "non-operate" region, and is unconstrained in
between (recorded as ``any``).
"""

from __future__ import annotations

import hashlib
import itertools
import random
from dataclasses import dataclass, replace
from typing import Iterator

from .events import DigitEnd, DigitStart
from .inband import InbandDtmfDetector
from .params import DIGITS, DtmfParams
from .synth import dtmf_case, stimulus_clips

ALL_DIGITS: tuple[str, ...] = tuple(d for row in DIGITS for d in row)

AXES: dict[str, tuple] = {
    "digit": ALL_DIGITS,
    "duration_ms": (23.0, 40.0, 50.0, 100.0, 300.0),
    "freq_dev": (0.0, 0.015, -0.015, 0.035, -0.035),
    "twist_db": (-12.0, -8.0, 0.0, 4.0, 8.0),
    "level_dbm0": (-3.0, -15.0, -25.0, -55.0),
    "codec": ("lin", "alaw", "ulaw"),
    "snr_db": (None, 20.0, 12.0),
}

# Q.24 operate / non-operate limits (Table A-1, AT&T column)
OPERATE_MIN_DURATION_MS = 40.0
NON_OPERATE_MAX_DURATION_MS = 23.0
OPERATE_MAX_DEV = 0.015
NON_OPERATE_MIN_DEV = 0.035
OPERATE_TWIST_DB = (-8.0, 4.0)
NON_OPERATE_TWIST_DB = (-12.0, 8.0)  # outside (exclusive) of this → must reject
OPERATE_MIN_LEVEL_DBM0 = -25.0
NON_OPERATE_MAX_LEVEL_DBM0 = -55.0


@dataclass(frozen=True)
class Case:
    digit: str = "5"
    duration_ms: float = 100.0
    freq_dev: float = 0.0
    twist_db: float = 0.0
    level_dbm0: float = -15.0
    codec: str = "lin"
    snr_db: float | None = None

    @property
    def expected(self) -> str:
        """``detect`` | ``reject`` | ``any``."""
        if (
            self.duration_ms <= NON_OPERATE_MAX_DURATION_MS
            or abs(self.freq_dev) >= NON_OPERATE_MIN_DEV
            or self.twist_db <= NON_OPERATE_TWIST_DB[0]
            or self.twist_db >= NON_OPERATE_TWIST_DB[1]
            or self.level_dbm0 <= NON_OPERATE_MAX_LEVEL_DBM0
        ):
            return "reject"
        if (
            self.duration_ms >= OPERATE_MIN_DURATION_MS
            and abs(self.freq_dev) <= OPERATE_MAX_DEV
            and OPERATE_TWIST_DB[0] <= self.twist_db <= OPERATE_TWIST_DB[1]
            and self.level_dbm0 >= OPERATE_MIN_LEVEL_DBM0
        ):
            return "detect"
        return "any"

    @property
    def clipped(self) -> bool:
        """The stimulus (tone + 4σ noise) exceeds the 16-bit range — not a valid Q.24 cell."""
        return stimulus_clips(
            self.digit, self.duration_ms, level_dbm0=self.level_dbm0, twist_db=self.twist_db, snr_db=self.snr_db
        )

    @property
    def effective_expected(self) -> str:
        return "clipped" if (self.expected == "detect" and self.clipped) else self.expected

    @property
    def seed(self) -> int:
        key = f"{self.digit}|{self.duration_ms}|{self.freq_dev}|{self.twist_db}|{self.level_dbm0}|{self.codec}|{self.snr_db}"
        return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")

    def label(self) -> str:
        snr = "inf" if self.snr_db is None else f"{self.snr_db:g}"
        return (
            f"{self.digit} {self.duration_ms:g}ms dev{self.freq_dev * 100:+g}% twist{self.twist_db:+g}dB "
            f"{self.level_dbm0:g}dBm0 {self.codec} snr{snr}"
        )


@dataclass(frozen=True)
class Outcome:
    case: Case
    digits: str  # confirmed digits in order
    starts: tuple[DigitStart, ...]
    ends: tuple[DigitEnd, ...]
    t_on: int
    t_off: int

    @property
    def passed(self) -> bool:
        exp = self.case.effective_expected
        if exp == "detect":
            return self.digits == self.case.digit
        if exp == "reject":
            return self.digits == ""
        return True  # "any" and "clipped" cells are informational


def run_case(case: Case, params: DtmfParams | None = None) -> Outcome:
    pcm, t_on, t_off = dtmf_case(
        case.digit,
        case.duration_ms,
        level_dbm0=case.level_dbm0,
        twist_db=case.twist_db,
        freq_dev=case.freq_dev,
        snr_db=case.snr_db,
        codec=case.codec,
        seed=case.seed,
    )
    det = InbandDtmfDetector(params)
    events = det.push(pcm) + det.finish()
    starts = tuple(e for e in events if isinstance(e, DigitStart))
    ends = tuple(e for e in events if isinstance(e, DigitEnd))
    return Outcome(case, "".join(e.digit for e in starts), starts, ends, t_on, t_off)


def full_factorial() -> Iterator[Case]:
    names = list(AXES)
    for values in itertools.product(*(AXES[n] for n in names)):
        yield Case(**dict(zip(names, values)))


def one_factor_at_a_time(base: Case | None = None) -> list[Case]:
    base = base or Case()
    cases: list[Case] = [base]
    for name, values in AXES.items():
        for v in values:
            c = replace(base, **{name: v})
            if c != base:
                cases.append(c)
    return cases


def random_subset(n: int, seed: int = 20261014) -> list[Case]:
    rng = random.Random(seed)
    names = list(AXES)
    return [Case(**{k: rng.choice(AXES[k]) for k in names}) for _ in range(n)]


def full_factorial_size() -> int:
    size = 1
    for v in AXES.values():
        size *= len(v)
    return size

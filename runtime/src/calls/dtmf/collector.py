"""``collect_digits`` — the scenario primitive for gathering a digit string.

Deterministic outcome on the media clock (docs/ARCHITECTURE.md §S3.4):

    reason ∈ {complete, terminator, timeout, cancel, hangup}
    ok      = len(digits) >= spec.min_digits   (for terminator/timeout outcomes)

``sensitive`` requests PCI handling by the orchestrator (pause recording and ASR, mask
digits in logs); the collector itself never logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CollectSpec:
    min_digits: int = 1
    max_digits: int = 16
    terminators: tuple[str, ...] = ("#",)
    cancel: str | None = "*"
    first_digit_timeout_ms: float = 5000.0
    inter_digit_timeout_ms: float = 3000.0
    sensitive: bool = False

    def __post_init__(self) -> None:
        if self.min_digits < 0 or self.max_digits < 1 or self.min_digits > self.max_digits:
            raise ValueError("invalid min/max digits")
        if self.cancel is not None and self.cancel in self.terminators:
            raise ValueError("cancel digit cannot also be a terminator")


@dataclass(frozen=True)
class CollectResult:
    digits: str
    reason: str
    ok: bool
    t_first: int | None
    t_last: int | None
    t_done: int


@dataclass
class DigitCollector:
    spec: CollectSpec
    t_begin: int
    sample_rate: int = 8000

    _digits: list[str] = field(default_factory=list, init=False)
    _t_first: int | None = field(default=None, init=False)
    _t_last: int | None = field(default=None, init=False)
    _done: CollectResult | None = field(default=None, init=False)

    def _ms(self, ms: float) -> int:
        return int(round(ms * self.sample_rate / 1000.0))

    @property
    def result(self) -> CollectResult | None:
        return self._done

    def _finish(self, reason: str, t: int) -> CollectResult:
        digits = "".join(self._digits)
        ok = reason == "complete" or (reason in ("terminator", "timeout") and len(digits) >= self.spec.min_digits)
        self._done = CollectResult(digits, reason, ok, self._t_first, self._t_last, t)
        return self._done

    def deadline(self) -> int:
        """Media time at which the current timeout fires."""
        if self._t_last is None:
            return self.t_begin + self._ms(self.spec.first_digit_timeout_ms)
        return self._t_last + self._ms(self.spec.inter_digit_timeout_ms)

    def on_digit(self, digit: str, t: int) -> CollectResult | None:
        if self._done is not None:
            return self._done
        if t >= self.deadline():
            return self._finish("timeout", self.deadline())
        if self.spec.cancel is not None and digit == self.spec.cancel:
            self._t_last = t
            return self._finish("cancel", t)
        if digit in self.spec.terminators:
            self._t_last = t
            return self._finish("terminator", t)
        self._digits.append(digit)
        if self._t_first is None:
            self._t_first = t
        self._t_last = t
        if len(self._digits) >= self.spec.max_digits:
            return self._finish("complete", t)
        return None

    def on_tick(self, t: int) -> CollectResult | None:
        if self._done is not None:
            return self._done
        if t >= self.deadline():
            return self._finish("timeout", self.deadline())
        return None

    def on_hangup(self, t: int) -> CollectResult:
        if self._done is not None:
            return self._done
        return self._finish("hangup", t)

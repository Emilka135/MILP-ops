"""In-band DTMF detector: a pure, replayable state machine over 8 kHz PCM.

Design (docs/ARCHITECTURE.md §S3.2):

* Identity — a 16-bin windowed DFT/Goertzel bank (8 Q.23 fundamentals + their 2nd
  harmonics) over N = 205-sample Hann windows advanced by ``hop`` = 20 samples (2.5 ms).
* Duration rule — the pair power of a window is proportional to (covered weight)², so the
  −6 dB (``edge_db``) contour of the power-vs-time curve is the 50 % coverage contour and
  its width equals the tone duration D for every D ≥ N (and saturates below N for shorter
  tones). A digit is *confirmed* once that width reaches ``min_duration_ms`` (32 ms): a
  23 ms tone is never confirmed, a 40 ms tone always is, independent of its alignment to
  the hop grid.
* Plateau — windows within ``plateau_tol_db`` of the burst maximum are "fully covered";
  the ITU-T Q.24 measurements (level, twist, dominance, harmonics, concentration,
  frequency tolerance from inter-window phase advance) are made on them only, with the
  kernel's detuning loss undone analytically, so partial coverage never biases a
  measurement.
* Search is stateless — every window re-derives the current burst from the window history
  (bounded by ``history_ms``), so a single noisy window never resets anything, and a burst
  that has just ended is still evaluated for ``tail_tolerance_ms`` with its full plateau.
* Timing — ``t_start`` is the −6 dB onset, ``t_end`` the first window below the edge (both
  window_start + N/2, accurate to one hop); interruptions shorter than ``bridge_ms`` are
  bridged.

Everything is a function of the sample stream and ``DtmfParams``: no wall clock, no
dependence on how the stream is framed. Feeding the same PCM in 20 ms or 100 ms chunks
yields bit-identical events (see tests/test_inband_timing.py).
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass
from enum import Enum

import numpy as np

from ..audio.level import amplitude_to_dbm0, dbm0_to_amplitude
from .bank import ToneBank
from .events import DigitEnd, DigitStart
from .params import DIGITS, HIGH_FREQS, LOW_FREQS, DtmfParams


class _State(Enum):
    SEARCH = 0
    ACTIVE = 1
    RELEASE = 2


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    level_low_dbm0: float = math.nan
    level_high_dbm0: float = math.nan
    twist_db: float = math.nan
    freq_dev_low: float = math.nan
    freq_dev_high: float = math.nan


def _trailing_run(mask: np.ndarray) -> int:
    """Length of the run of True values at the end of ``mask``."""
    if mask.size == 0:
        return 0
    rev = mask[::-1]
    if rev.all():
        return int(rev.size)
    return int(np.argmin(rev))


class InbandDtmfDetector:
    """Push PCM, receive :class:`DigitStart` / :class:`DigitEnd` events."""

    def __init__(self, params: DtmfParams | None = None) -> None:
        p = params or DtmfParams()
        self.p = p
        fund = tuple(LOW_FREQS) + tuple(HIGH_FREQS)
        self._freqs = fund + tuple(2.0 * f for f in fund)
        self.bank = ToneBank(self._freqs, p.sample_rate, p.window)

        a_floor = dbm0_to_amplitude(p.level_min_dbm0)
        self._floor_power = a_floor * a_floor / 2.0
        a_anchor = dbm0_to_amplitude(p.level_min_dbm0 - p.anchor_margin_db)
        self._anchor_power = a_anchor * a_anchor / 2.0
        self._dom_lin = 10.0 ** (p.dominance_db / 10.0)
        self._twist_min_lin = 10.0 ** ((p.twist_min_db - p.measurement_margin_db) / 10.0)
        self._twist_max_lin = 10.0 ** ((p.twist_max_db + p.measurement_margin_db) / 10.0)
        self._harm_lin = 10.0 ** (p.harmonic_max_db / 10.0)
        self._plateau_lin = 10.0 ** (-p.plateau_tol_db / 10.0)
        self._edge_lin = 10.0 ** (-p.edge_db / 10.0)
        self._bridge_samples = int(round(p.bridge_ms * p.sample_rate / 1000.0))
        self._min_duration_samples = int(round(p.min_duration_ms * p.sample_rate / 1000.0))
        self._tail_windows = int(round(p.tail_tolerance_ms / p.hop_ms))
        self._derot = np.array(
            [cmath.exp(-2j * math.pi * f * p.hop / p.sample_rate) for f in fund], dtype=np.complex128
        )

        # ring buffers of per-window measurements (window k lives at k % H)
        self._H = int(math.ceil(p.history_ms / p.hop_ms)) + self._tail_windows + 8
        self._pow = np.zeros((self._H, 16), dtype=np.float64)
        self._spec = np.zeros((self._H, 16), dtype=np.complex128)
        self._total = np.zeros(self._H, dtype=np.float64)
        self._start = np.zeros(self._H, dtype=np.int64)
        self._pair = np.zeros((self._H, 2), dtype=np.int64)
        self._lvl = np.zeros(self._H, dtype=bool)
        # every window is analysed from this one owned, aligned buffer so that BLAS sees the
        # same memory layout regardless of how the caller framed the stream (bit-exactness)
        self._frame = np.zeros(p.window, dtype=np.float64)

        # stream state
        self._buf = np.zeros(0, dtype=np.float64)
        self._pos = 0  # absolute index of self._buf[0]
        self._k = 0  # next window counter
        self._consumed_k = -1  # windows up to here belong to an already reported digit
        self._state = _State.SEARCH
        # active digit
        self._digit = ""
        self._digit_pair = (0, 0)
        self._t_start = 0
        self._p_plateau = 0.0
        self._release_start = 0
        self._t_end_est = 0

    # ------------------------------------------------------------------ public API
    @property
    def position(self) -> int:
        """Number of samples consumed so far (media clock of the next sample)."""
        return self._pos + len(self._buf)

    @property
    def active_digit(self) -> str | None:
        return self._digit if self._state in (_State.ACTIVE, _State.RELEASE) else None

    def push(self, pcm) -> list[DigitStart | DigitEnd]:
        """Feed samples (int16/float array or PCM16-LE bytes). Returns events in order."""
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        else:
            x = np.asarray(pcm, dtype=np.float64).reshape(-1)
        if x.size == 0:
            return []
        buf = np.concatenate([self._buf, x]) if self._buf.size else x
        n, hop = self.p.window, self.p.hop
        events: list[DigitStart | DigitEnd] = []
        off = 0
        while buf.size - off >= n:
            events.extend(self._step(buf[off : off + n], self._pos))
            off += hop
            self._pos += hop
        self._buf = buf[off:].copy() if off else buf
        return events

    def finish(self) -> list[DigitEnd]:
        """End of stream: close an active digit (its end is the last consumed sample)."""
        events: list[DigitEnd] = []
        if self._state is _State.ACTIVE:
            t_end = self.position
            events.append(DigitEnd(self._digit, self._t_start, t_end, t_end, self.p.version))
        elif self._state is _State.RELEASE:
            events.append(
                DigitEnd(self._digit, self._t_start, self._t_end_est, self.position, self.p.version)
            )
        self._state = _State.SEARCH
        self._consumed_k = self._k - 1
        return events

    # ------------------------------------------------------------------ core
    def _step(self, frame: np.ndarray, start: int) -> list[DigitStart | DigitEnd]:
        p = self.p
        f = self._frame
        np.copyto(f, frame)
        f -= f.mean()
        spectrum = self.bank.analyze(f)
        power = self.bank.tone_power(spectrum)
        li = int(np.argmax(power[0:4]))
        hi = int(np.argmax(power[4:8]))
        k = self._k
        self._k += 1
        slot = k % self._H
        self._pow[slot] = power
        self._spec[slot] = spectrum
        self._total[slot] = self.bank.frame_power(f)
        self._start[slot] = start
        self._pair[slot] = (li, hi)
        self._lvl[slot] = power[li] >= self._anchor_power and power[4 + hi] >= self._anchor_power
        end = start + p.window
        events: list[DigitStart | DigitEnd] = []

        if self._state is not _State.SEARCH:
            self._track(k, end, events)
            if self._state is not _State.SEARCH:
                return events
        self._search(k, end, events)
        return events

    def _continues(self, k: int) -> bool:
        """Continuation rule of an active digit for window ``k``."""
        slot = k % self._H
        li, hi = self._digit_pair
        pp = self._pow[slot, li] + self._pow[slot, 4 + hi]
        return (int(self._pair[slot, 0]), int(self._pair[slot, 1])) == self._digit_pair and pp >= self._p_plateau * self._edge_lin

    def _track(self, k: int, end: int, events: list) -> None:
        """ACTIVE/RELEASE bookkeeping for window ``k`` (may emit DigitEnd)."""
        p = self.p
        slot = k % self._H
        start = int(self._start[slot])
        if self._state is _State.ACTIVE:
            if not self._continues(k):
                self._state = _State.RELEASE
                self._release_start = start
                self._t_end_est = start + p.window // 2
        if self._state is _State.RELEASE:
            if self._continues(k):
                self._state = _State.ACTIVE  # interruption bridged
            elif start - self._release_start >= self._bridge_samples:
                events.append(DigitEnd(self._digit, self._t_start, self._t_end_est, end, p.version))
                self._state = _State.SEARCH
                self._consumed_k = k

    def _search(self, k: int, end: int, events: list) -> None:
        """Stateless burst search over the window history (see module docstring)."""
        p = self.p
        H = self._H
        first = max(self._consumed_k + 1, k - H + 1)
        m = k - first + 1
        if m < 2:
            return
        ks = np.arange(first, k + 1)
        idx = ks % H
        lvl = self._lvl[idx]
        tail_from = max(0, m - 1 - self._tail_windows)
        tail_hits = np.flatnonzero(lvl[tail_from:])
        if tail_hits.size == 0:
            return
        a = tail_from + int(tail_hits[-1])  # anchor: newest window whose own pair clears the anchor floor
        li, hi = int(self._pair[idx[a], 0]), int(self._pair[idx[a], 1])
        pw = self._pow[idx[: a + 1]]
        pp = pw[:, li] + pw[:, 4 + hi]
        # pass 1 — burst extent: walk back from the anchor while the pair power stays within
        # edge_db of the loudest window seen so far (stops at the gap before any older burst)
        rev = pp[::-1]
        run_max = np.maximum.accumulate(rev)
        in_burst = rev >= run_max * self._edge_lin
        blen = int(rev.size) if in_burst.all() else int(np.argmin(in_burst))
        p_max = float(run_max[blen - 1])
        # pass 2 — the −6 dB width relative to the burst maximum, contiguous with the anchor
        thr = p_max * self._edge_lin
        width = _trailing_run(pp >= thr)
        if width * p.hop < self._min_duration_samples:
            return
        onset = a + 1 - width
        plateau = onset + np.flatnonzero(pp[onset : a + 1] >= p_max * self._plateau_lin)
        if plateau.size < p.min_plateau_windows:
            return
        verdict = self._evaluate(idx[plateau], plateau, (li, hi))
        if not verdict.ok:
            return

        self._digit = DIGITS[li][hi]
        self._digit_pair = (li, hi)
        self._t_start = int(self._start[idx[onset]]) + p.window // 2
        self._p_plateau = float(np.mean(pp[plateau]))
        self._state = _State.ACTIVE
        events.append(
            DigitStart(
                digit=self._digit,
                t_start=self._t_start,
                t_detect=end,
                level_low_dbm0=verdict.level_low_dbm0,
                level_high_dbm0=verdict.level_high_dbm0,
                twist_db=verdict.twist_db,
                freq_dev_low=verdict.freq_dev_low,
                freq_dev_high=verdict.freq_dev_high,
                detector_version=p.version,
            )
        )
        # late confirmation (anchor older than the current window): replay the windows after
        # the anchor through the continuation rule so release timing stays on the media clock
        for kk in range(int(ks[a]) + 1, k + 1):
            self._track(kk, end, events)
            if self._state is _State.SEARCH:
                break

    # ------------------------------------------------------------------ Q.24 checks
    def _frequency_deviation(self, slots: np.ndarray, pos: np.ndarray, tone: int, f_nom: float) -> float | None:
        """Relative deviation from nominal, from the phase advance between consecutive windows:
        X_k · conj(X_{k−1}) · e^{−j2π f_nom hop/fs} = |C|² e^{j2π Δf hop/fs}."""
        consecutive = np.flatnonzero(np.diff(pos) == 1)
        if consecutive.size == 0:
            return None
        cur = self._spec[slots[consecutive + 1], tone]
        prev = self._spec[slots[consecutive], tone]
        acc = complex(np.sum(cur * np.conj(prev)) * self._derot[tone])
        dev_hz = cmath.phase(acc) * self.p.sample_rate / (2.0 * math.pi * self.p.hop)
        return dev_hz / f_nom

    def _evaluate(self, slots: np.ndarray, pos: np.ndarray, pair: tuple[int, int]) -> Verdict:
        """Apply the Q.24 receiver criteria to the fully-covered windows of a burst."""
        p = self.p
        li, hi = pair
        f_l, f_h = LOW_FREQS[li], HIGH_FREQS[hi]

        # frequency first: the edge windows of the plateau may be slightly truncated, so
        # prefer the interior ones for the phase measurement when there are enough.
        if slots.size >= 4:
            dev_l = self._frequency_deviation(slots[1:-1], pos[1:-1], li, f_l)
            dev_h = self._frequency_deviation(slots[1:-1], pos[1:-1], 4 + hi, f_h)
        else:
            dev_l = dev_h = None
        if dev_l is None or dev_h is None:
            dev_l = self._frequency_deviation(slots, pos, li, f_l)
            dev_h = self._frequency_deviation(slots, pos, 4 + hi, f_h)
        if dev_l is None or dev_h is None:
            return Verdict(False, "frequency")
        if abs(dev_l) > p.freq_tol or abs(dev_h) > p.freq_tol:
            return Verdict(False, "frequency")

        mean_p = self._pow[slots].mean(axis=0)
        raw_l = float(mean_p[li])
        raw_h = float(mean_p[4 + hi])
        # undo the kernel's detuning loss (≤ 2.3 dB at ±1.5 %, bounded by freq_tol)
        p_l = raw_l / self.bank.kernel_gain(dev_l * f_l)
        p_h = raw_h / self.bank.kernel_gain(dev_h * f_h)

        if p_l < self._floor_power or p_h < self._floor_power:
            return Verdict(False, "level")
        twist = p_h / p_l
        if not (self._twist_min_lin <= twist <= self._twist_max_lin):
            return Verdict(False, "twist")
        low_other = max(float(mean_p[j]) for j in range(4) if j != li)
        high_other = max(float(mean_p[4 + j]) for j in range(4) if j != hi)
        if p_l < self._dom_lin * low_other or p_h < self._dom_lin * high_other:
            return Verdict(False, "dominance")
        harm_limit = self._harm_lin * (raw_l + raw_h)
        for tone in (li, 4 + hi):
            fh = self._freqs[8 + tone]
            if abs(fh - f_l) < p.harmonic_guard_hz or abs(fh - f_h) < p.harmonic_guard_hz:
                continue
            if float(mean_p[8 + tone]) > harm_limit:
                return Verdict(False, "harmonic")
        p_total = float(self._total[slots].mean())
        if p_total <= 0.0 or (p_l + p_h) / p_total < p.concentration_min:
            return Verdict(False, "concentration")

        return Verdict(
            True,
            "ok",
            level_low_dbm0=amplitude_to_dbm0(self.bank.power_to_amplitude(p_l)),
            level_high_dbm0=amplitude_to_dbm0(self.bank.power_to_amplitude(p_h)),
            twist_db=10.0 * math.log10(twist),
            freq_dev_low=dev_l,
            freq_dev_high=dev_h,
        )

"""DTMF constants (ITU-T Q.23) and detector parameters (ITU-T Q.24 receiver criteria).

Every threshold below is a *receiver* criterion from ITU-T Recommendation Q.24
(Table A-1, AT&T column) unless stated otherwise. The temporal design (window /
hop / plateau rule) is ours; its justification is in docs/ARCHITECTURE.md §S3.2.
"""

from __future__ import annotations

from dataclasses import dataclass

LOW_FREQS: tuple[float, ...] = (697.0, 770.0, 852.0, 941.0)
HIGH_FREQS: tuple[float, ...] = (1209.0, 1336.0, 1477.0, 1633.0)

# DIGITS[low_index][high_index]
DIGITS: tuple[tuple[str, ...], ...] = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)

# RFC 4733 event codes 0..15 -> digit
RFC4733_EVENTS: tuple[str, ...] = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "#", "A", "B", "C", "D")


def digit_to_pair(digit: str) -> tuple[int, int]:
    for li, row in enumerate(DIGITS):
        for hi, d in enumerate(row):
            if d == digit:
                return li, hi
    raise ValueError(f"not a DTMF digit: {digit!r}")


@dataclass(frozen=True)
class DtmfParams:
    """Deterministic detector configuration. The ``version`` string is stamped on events."""

    sample_rate: int = 8000
    window: int = 205
    """Analysis window N (25.6 ms @ 8 kHz): the classic choice placing DFT bins near the
    Q.23 nominals with enough resolution for the ±1.5 % tolerance test."""
    hop: int = 20
    """Hop between analysis windows (2.5 ms): sets timing resolution of start/end."""

    # --- Q.24 per-window / per-plateau acceptance criteria -------------------------
    level_min_dbm0: float = -36.0
    """Per-tone level floor. Q.24: operation at 0 … −25 dBm0, non-operation ≤ −55 dBm0.
    −36 leaves 11 dB for line loss while still rejecting −55 by a wide margin."""
    anchor_margin_db: float = 6.0
    """A window is a search anchor when both tones of its arg-max pair exceed
    ``level_min_dbm0 − anchor_margin_db`` (raw, per window). The authoritative level test is
    made on the plateau mean with the kernel loss corrected, so this only widens the search."""
    twist_min_db: float = -8.0
    twist_max_db: float = 4.0
    """Twist = level(high) − level(low). Q.24 (AT&T): +4 … −8 dB — an *operate* range, so the
    decision uses it widened by ``measurement_margin_db`` (Q.24 has no reject bound here)."""
    measurement_margin_db: float = 1.0
    """Allowance for residual measurement error (codec, noise, kernel detuning) at the Q.24
    operate boundaries: applied to the twist window."""
    dominance_db: float = 8.0
    """Strongest tone in each group must exceed the runner-up by this much."""
    harmonic_max_db: float = -15.0
    """2nd harmonic of either selected fundamental relative to the total pair power (voiced
    speech carries strong even harmonics; a DTMF generator does not)."""
    harmonic_guard_hz: float = 150.0
    """A harmonic bin closer than this to either selected fundamental is not evaluated
    (e.g. 2×697 Hz = 1394 Hz sits 58 Hz from 1336 Hz and would be polluted by leakage)."""
    concentration_min: float = 0.75
    """(E_low + E_high) / E_window — a pure dual tone is ≈ 1.0; tolerates SNR ≈ 5 dB."""
    freq_tol: float = 0.025
    """Relative frequency tolerance decision boundary: Q.24 says accept ≤ 1.5 %, reject
    ≥ 3.5 %; 2.5 % is the midpoint. Frequency is measured from inter-window phase advance."""

    # --- temporal design -------------------------------------------------------------
    edge_db: float = 6.0
    """Pair energy −6 dB == 50 % window coverage. The width of the energy curve at −6 dB
    equals max(D, N) for a tone of duration D, so it measures D (to within one hop) for
    every tone longer than the window and saturates at N = 25.6 ms for shorter ones."""
    min_duration_ms: float = 32.0
    """Confirmation threshold on the −6 dB width. Q.24: operate at 40 ms, do not operate at
    23 ms; 32 ms sits between them with ≥ 6 ms margin on both sides after hop quantisation.
    DigitStart latency after onset ≈ min_duration + N/2 + hop ≈ 47 ms."""
    plateau_tol_db: float = 1.0
    """Windows within this of the run maximum are 'fully covered'; the Q.24 measurements
    (level, twist, dominance, harmonics, frequency) are made on them only."""
    min_plateau_windows: int = 2
    """Frequency is measured from the phase advance between consecutive plateau windows."""
    bridge_ms: float = 20.0
    """Interruptions shorter than this inside a tone are bridged (ETSI ES 201 235-3)."""
    tail_tolerance_ms: float = 15.0
    """After a burst ends, its full plateau is still evaluated for this long — the rescue
    path for marginal (noisy) 40 ms tones; DigitStart is correspondingly later."""
    history_ms: float = 400.0
    """Bound on the window history searched for a burst (and on the walk-back)."""

    version: str = "goertzel-q24-1.0"

    def __post_init__(self) -> None:
        if self.hop <= 0 or self.window <= 0 or self.hop > self.window:
            raise ValueError("window and hop must be positive with hop <= window")
        if self.min_plateau_windows < 2:
            raise ValueError("min_plateau_windows must be >= 2 (frequency estimation needs two windows)")
        if self.min_duration_ms <= self.window_ms:
            raise ValueError("min_duration_ms must exceed the window length (the -6 dB width saturates at N)")
        if self.twist_min_db > self.twist_max_db:
            raise ValueError("twist_min_db must not exceed twist_max_db")

    @property
    def hop_ms(self) -> float:
        return 1000.0 * self.hop / self.sample_rate

    @property
    def window_ms(self) -> float:
        return 1000.0 * self.window / self.sample_rate

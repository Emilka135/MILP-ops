"""Tone bank: equivalence with the textbook Goertzel recurrence, calibration, kernel gain."""

import math

import numpy as np
import pytest

from calls.audio.level import amplitude_to_dbm0, dbm0_to_amplitude
from calls.dtmf.bank import ToneBank, goertzel_reference
from calls.dtmf.params import HIGH_FREQS, LOW_FREQS

FS = 8000
N = 205


def test_rect_bank_equals_goertzel_recurrence():
    rng = np.random.default_rng(3)
    x = rng.normal(0, 1000, N)
    bank = ToneBank(LOW_FREQS + HIGH_FREQS, FS, N, taper="rect")
    X = bank.analyze(x)
    for i, f in enumerate(LOW_FREQS + HIGH_FREQS):
        ref = goertzel_reference(x, f, FS)
        assert abs(X[i] - ref) <= 1e-6 * abs(ref) + 1e-6


@pytest.mark.parametrize("taper", ["rect", "hann"])
@pytest.mark.parametrize("dbm0", [0.0, -10.0, -25.0, -40.0])
def test_tone_power_calibration(taper, dbm0):
    bank = ToneBank([1336.0], FS, N, taper=taper)
    a = dbm0_to_amplitude(dbm0)
    n = np.arange(N)
    best = -math.inf
    for phase in (0.0, 1.0, 2.5):
        x = a * np.cos(2 * math.pi * 1336.0 * n / FS + phase)
        p = float(bank.tone_power(bank.analyze(x))[0])
        level = amplitude_to_dbm0(bank.power_to_amplitude(p))
        best = max(best, abs(level - dbm0))
    assert best < 0.2, f"calibration error {best:.3f} dB"


def test_hann_kernel_gain_matches_q24_tolerances():
    bank = ToneBank([1633.0], FS, N)
    assert bank.kernel_gain(0.0) == pytest.approx(1.0)
    loss_1_5 = -10 * math.log10(bank.kernel_gain(0.015 * 1633.0))
    loss_3_5 = -10 * math.log10(bank.kernel_gain(0.035 * 1633.0))
    assert loss_1_5 < 2.5  # Q.24 operate band: small, analytically corrected bias
    assert loss_3_5 > 10.0  # non-operate band: strongly attenuated


def test_hann_sidelobes_separate_adjacent_dtmf_tones():
    """697 vs 770 Hz is the closest pair (73 Hz): leakage must stay far below the 8 dB
    dominance margin with an extra 6 dB of safety."""
    bank = ToneBank(LOW_FREQS, FS, N)
    n = np.arange(N)
    x = np.cos(2 * math.pi * 697.0 * n / FS)
    p = bank.tone_power(bank.analyze(x))
    leak_db = 10 * math.log10(p[1] / p[0])
    assert leak_db < -14.0


def test_analyze_rejects_wrong_shape():
    bank = ToneBank([697.0], FS, N)
    with pytest.raises(ValueError):
        bank.analyze(np.zeros(N - 1))

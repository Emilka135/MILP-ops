"""Tone bank: windowed DFT at arbitrary frequencies (what the Goertzel algorithm computes).

The matrix form (one dot product per analysis window) is used in production because it
is vectorised and shape-invariant: a window is always analysed as a single
``(N,) @ (N, F)`` product, so the result does not depend on how the stream was framed
(20 ms or 100 ms pushes give bit-identical numbers). ``goertzel_reference`` is the
textbook recurrence, kept for equivalence tests and as the spec for ports to C/Rust/Go.

Why a Hann taper (not the textbook rectangular Goertzel window): ITU-T Q.24 requires
operation at ±1.5 % frequency deviation. For 1633 Hz that is ±24.5 Hz, while the
rectangular main lobe of a 205-sample window has its first null at 39 Hz, so the
measured level would drop by 6 dB and corrupt the twist test. With a Hann taper the
loss at ±24.5 Hz is 2.3 dB, sidelobes are −31 dB (better talk-off), and the inter-window
phase used for frequency estimation is insensitive to partial coverage at the edges.
The residual bias is corrected analytically via :meth:`ToneBank.kernel_gain`.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Sequence

import numpy as np


class ToneBank:
    def __init__(self, freqs: Sequence[float], sample_rate: int, window: int, taper: str = "hann") -> None:
        self.freqs = np.asarray(freqs, dtype=np.float64)
        self.sample_rate = int(sample_rate)
        self.window = int(window)
        n = np.arange(self.window, dtype=np.float64)
        if taper == "hann":
            self.w = 0.5 - 0.5 * np.cos(2.0 * math.pi * (n + 1.0) / (self.window + 1.0))
        elif taper == "rect":
            self.w = np.ones(self.window)
        else:
            raise ValueError(f"unknown taper {taper!r}")
        self.s1 = float(np.sum(self.w))  # Σ w
        self.s2 = float(np.sum(self.w * self.w))  # Σ w²
        # basis[n, f] = w[n] · exp(-j 2π f n / fs)  ->  X[f] = Σ w[n] x[n] e^{-jωn}
        self.basis = self.w[:, None] * np.exp(-2j * math.pi * np.outer(n, self.freqs) / self.sample_rate)

    def analyze(self, frame: np.ndarray) -> np.ndarray:
        """Complex windowed DFT values (F,) of one real frame of exactly ``window`` samples."""
        if frame.shape != (self.window,):
            raise ValueError(f"frame must have shape ({self.window},), got {frame.shape}")
        return frame @ self.basis

    def tone_power(self, x: np.ndarray) -> np.ndarray:
        """Mean-square (power) of a sinusoid seen through bin X: A = 2|X|/S1, P = A²/2."""
        return 2.0 * (x.real * x.real + x.imag * x.imag) / (self.s1 * self.s1)

    def frame_power(self, frame: np.ndarray) -> float:
        """Mean-square of the frame weighted like the tone bins: Σ (w x)² / Σ w²."""
        f = frame * self.w
        return float(f @ f) / self.s2

    def kernel_gain(self, delta_hz: float) -> float:
        """Power response of the analysis kernel at ``delta_hz`` off the bin centre, relative
        to on-centre (1.0). Used to undo the level bias of a slightly detuned tone."""
        n = np.arange(self.window, dtype=np.float64)
        k = np.sum(self.w * np.exp(-2j * math.pi * delta_hz * n / self.sample_rate))
        return float((k.real * k.real + k.imag * k.imag) / (self.s1 * self.s1))

    @staticmethod
    def power_to_amplitude(power: float) -> float:
        return math.sqrt(max(power, 0.0) * 2.0)


def goertzel_reference(x: Sequence[float], freq: float, sample_rate: int) -> complex:
    """Textbook Goertzel recurrence returning the DFT value Σ x[n] e^{-jωn} (no taper).

    The recurrence yields y = s1 − e^{-jω} s2 = e^{jω(N−1)} · X(ω); the final rotation
    removes that factor so the result is directly comparable with a rectangular
    :meth:`ToneBank.analyze`.
    """
    w = 2.0 * math.pi * freq / sample_rate
    coeff = 2.0 * math.cos(w)
    s1 = 0.0
    s2 = 0.0
    n = 0
    for v in x:
        s0 = float(v) + coeff * s1 - s2
        s2 = s1
        s1 = s0
        n += 1
    y = s1 - cmath.exp(-1j * w) * s2
    return y * cmath.exp(-1j * w * (n - 1))

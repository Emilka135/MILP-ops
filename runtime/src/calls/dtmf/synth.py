"""DTMF signal synthesis for acceptance tests and the lab stand.

All generators are deterministic (seeded) and parameterised exactly along the ITU-T Q.24
axes used by the acceptance matrix: duration, frequency deviation, twist, level, codec,
and additive white noise at a given SNR.
"""

from __future__ import annotations

import math

import numpy as np

from ..audio.g711 import alaw_decode, alaw_encode, ulaw_decode, ulaw_encode
from ..audio.level import dbm0_to_amplitude
from .params import HIGH_FREQS, LOW_FREQS, digit_to_pair

CODECS = ("lin", "alaw", "ulaw")


def apply_codec(pcm: np.ndarray, codec: str) -> np.ndarray:
    """Round-trip int16 PCM through a G.711 codec (or return it unchanged for ``lin``)."""
    x = np.clip(np.rint(pcm), -32768, 32767).astype(np.int16)
    if codec == "lin":
        return x
    if codec == "alaw":
        return alaw_decode(alaw_encode(x))
    if codec == "ulaw":
        return ulaw_decode(ulaw_encode(x))
    raise ValueError(f"unknown codec {codec!r}")


def dtmf_tone(
    digit: str,
    duration_ms: float,
    *,
    sample_rate: int = 8000,
    level_dbm0: float = -10.0,
    twist_db: float = 0.0,
    freq_dev: float = 0.0,
    phase: tuple[float, float] = (0.0, 0.0),
) -> np.ndarray:
    """Float64 dual tone. ``level_dbm0`` is the level of the *louder* tone; twist = high − low
    decides which one that is, so the peak never exceeds 2 × amplitude(level_dbm0)."""
    li, hi = digit_to_pair(digit)
    f_low = LOW_FREQS[li] * (1.0 + freq_dev)
    f_high = HIGH_FREQS[hi] * (1.0 + freq_dev)
    if twist_db >= 0.0:
        high_dbm0, low_dbm0 = level_dbm0, level_dbm0 - twist_db
    else:
        low_dbm0, high_dbm0 = level_dbm0, level_dbm0 + twist_db
    n = int(round(duration_ms * sample_rate / 1000.0))
    t = np.arange(n, dtype=np.float64) / sample_rate
    return dbm0_to_amplitude(low_dbm0) * np.cos(2 * math.pi * f_low * t + phase[0]) + dbm0_to_amplitude(
        high_dbm0
    ) * np.cos(2 * math.pi * f_high * t + phase[1])


def with_noise(signal: np.ndarray, snr_db: float | None, *, reference_power: float, seed: int) -> np.ndarray:
    """Add white Gaussian noise with power ``reference_power / 10^(snr/10)``."""
    if snr_db is None or math.isinf(snr_db):
        return signal
    rng = np.random.default_rng(seed)
    noise_power = reference_power / (10.0 ** (snr_db / 10.0))
    return signal + rng.normal(0.0, math.sqrt(noise_power), size=signal.shape)


def dtmf_case(
    digit: str,
    duration_ms: float,
    *,
    sample_rate: int = 8000,
    level_dbm0: float = -10.0,
    twist_db: float = 0.0,
    freq_dev: float = 0.0,
    snr_db: float | None = None,
    codec: str = "lin",
    lead_ms: float = 100.0,
    trail_ms: float = 100.0,
    seed: int = 0,
) -> tuple[np.ndarray, int, int]:
    """One test case: silence, tone, silence (+ noise, + codec). Returns (pcm, t_on, t_off)."""
    lead = int(round(lead_ms * sample_rate / 1000.0))
    tone = dtmf_tone(
        digit, duration_ms, sample_rate=sample_rate, level_dbm0=level_dbm0, twist_db=twist_db, freq_dev=freq_dev
    )
    trail = int(round(trail_ms * sample_rate / 1000.0))
    sig = np.concatenate([np.zeros(lead), tone, np.zeros(trail)])
    tone_power = float(np.mean(tone * tone)) if tone.size else 0.0
    sig = with_noise(sig, snr_db, reference_power=tone_power, seed=seed)
    return apply_codec(sig, codec), lead, lead + tone.size


def stimulus_clips(
    digit: str,
    duration_ms: float,
    *,
    level_dbm0: float,
    twist_db: float,
    snr_db: float | None,
    sample_rate: int = 8000,
) -> bool:
    """True when the dual tone plus 4σ of the requested noise exceeds the 16-bit range, i.e.
    the stimulus itself is outside what a G.711 channel can carry (such matrix cells are
    reported as ``clipped`` rather than counted as detector failures)."""
    tone = dtmf_tone(digit, max(duration_ms, 1.0), sample_rate=sample_rate, level_dbm0=level_dbm0, twist_db=twist_db)
    peak = float(np.max(np.abs(tone)))
    if snr_db is not None and not math.isinf(snr_db):
        noise_power = float(np.mean(tone * tone)) / (10.0 ** (snr_db / 10.0))
        peak += 4.0 * math.sqrt(noise_power)
    return peak > 32767.0


def dtmf_sequence(
    digits: str,
    *,
    on_ms: float = 100.0,
    off_ms: float = 100.0,
    sample_rate: int = 8000,
    level_dbm0: float = -10.0,
    lead_ms: float = 100.0,
    trail_ms: float = 100.0,
    codec: str = "lin",
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """A dialled string; returns (pcm, [(t_on, t_off), ...])."""
    parts = [np.zeros(int(round(lead_ms * sample_rate / 1000.0)))]
    spans = []
    pos = parts[0].size
    gap = np.zeros(int(round(off_ms * sample_rate / 1000.0)))
    for i, d in enumerate(digits):
        tone = dtmf_tone(d, on_ms, sample_rate=sample_rate, level_dbm0=level_dbm0)
        spans.append((pos, pos + tone.size))
        parts.append(tone)
        pos += tone.size
        if i != len(digits) - 1:
            parts.append(gap)
            pos += gap.size
    parts.append(np.zeros(int(round(trail_ms * sample_rate / 1000.0))))
    return apply_codec(np.concatenate(parts), codec), spans


def speech_like(duration_ms: float, *, sample_rate: int = 8000, seed: int = 1, level_dbm0: float = -12.0) -> np.ndarray:
    """A crude voiced-speech surrogate (harmonic series with pitch glides and syllabic
    amplitude modulation). Not a substitute for a talk-off corpus; used as a smoke test."""
    rng = np.random.default_rng(seed)
    n = int(round(duration_ms * sample_rate / 1000.0))
    t = np.arange(n) / sample_rate
    out = np.zeros(n)
    seg = int(0.25 * sample_rate)
    for s in range(0, n, seg):
        e = min(seg, n - s)
        tt = t[:e]
        f0 = rng.uniform(90.0, 260.0)
        glide = rng.uniform(-0.3, 0.3)
        pitch = f0 * (1.0 + glide * tt / tt[-1])
        ph = 2 * math.pi * np.cumsum(pitch) / sample_rate
        frame = np.zeros(e)
        for h in range(1, 12):
            frame += (1.0 / h) * np.cos(h * ph + rng.uniform(0, 2 * math.pi))
        env = 0.5 - 0.5 * np.cos(2 * math.pi * tt / (e / sample_rate))
        out[s : s + e] = frame * env
    out *= dbm0_to_amplitude(level_dbm0) / (np.max(np.abs(out)) + 1e-9)
    return out

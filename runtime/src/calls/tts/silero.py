"""Silero TTS wrapper (``torch.package`` models) with word stress, exact speed control and a prompt cache.

* Default model: **``v5_cis_base.pt``** (MIT, ``LICENSE_CIS`` in snakers4/silero-models; speakers ``ru_*``,
  synthesized at 48 kHz, anti-aliased conversion to 8 kHz for calls). It has no auto-stress, so prompts go through
  :mod:`calls.tts.stress` (built-in lexicon for the whole prompt vocabulary, optional
  ``silero-stress`` fallback for names from the Excel schedule).
* ``silero-v5_5_ru.pt`` (speakers aidar/baya/kseniya/xenia/eugene) still works via ``TTS_MODEL``,
  but it is CC BY-NC-SA 4.0 – a commercial licence is needed outside the hackathon (docs/LICENSES.md).
* Speed 1.3–1.75×: neutral model prosody, FFmpeg atempo at 48 kHz, then telephony conversion.
  The legacy WSOLA helper remains for compatibility tests; live speech does not use it.
* Prompts repeat heavily (menus, confirmations) → ``TtsModule`` keeps an LRU cache keyed by (text, rate).
* :class:`StubTTS` is only a unit-test dependency. Live ``clinic.Speech`` requires real weights
  and fails at startup if they are absent; audio acceptance uses no silence fallback.
"""

from __future__ import annotations

import hashlib
import re
import threading
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from .stress import Accentor, NoStress, load_accentor, accentor_fingerprint, require_stress_marks
from .verbalize import rate_hint
from .normalize import normalize_text, NORMALIZER_VERSION
from .processing import process_audio, SOURCE_RATE, PROCESSING_VERSION

SAMPLE_RATE = 8000
DEFAULT_MODEL = "modules/tts/models/v5_cis_base.pt"
DEFAULT_SPEAKER_CIS = "ru_ekaterina"  # Selected by the real phrase/number regressions; retain ru_* voice IDs
DEFAULT_SPEAKER_RU = "xenia"


def wsola_stretch(x: np.ndarray, factor: float, sr: int = SAMPLE_RATE, frame_ms: float = 25.0, tolerance_ms: float = 5.0) -> np.ndarray:
    """Time-scale ``x`` (float32 mono) by ``factor`` (>1 = faster/shorter) preserving pitch (WSOLA)."""
    if factor <= 0:
        raise ValueError("factor must be positive")
    if abs(factor - 1.0) < 1e-3 or len(x) < int(sr * frame_ms / 1000) * 2:
        return x.astype(np.float32, copy=False)
    n = int(sr * frame_ms / 1000)
    hop_out = n // 2
    hop_in = hop_out * factor
    tol = int(sr * tolerance_ms / 1000)
    win = np.hanning(n).astype(np.float32)
    out_len = int(len(x) / factor) + n
    out = np.zeros(out_len, dtype=np.float32)
    norm = np.zeros(out_len, dtype=np.float32)
    # first frame
    out[:n] += x[:n] * win
    norm[:n] += win
    prev_end = n  # natural continuation of the previous frame in the input
    k = 1
    while True:
        pos_out = k * hop_out
        if pos_out + n > out_len:
            break
        center = int(round(k * hop_in))
        if center + n + tol > len(x):
            break
        # candidate region around the nominal position; choose the shift best matching the natural continuation
        nat = x[prev_end - hop_out : prev_end - hop_out + n] if prev_end - hop_out >= 0 else None
        lo = max(0, center - tol)
        hi = min(len(x) - n, center + tol)
        if nat is None or hi <= lo:
            best = center
        else:
            seg = x[lo : hi + n]
            # cross-correlation via sliding dot products
            windows = np.lib.stride_tricks.sliding_window_view(seg, n)[: hi - lo + 1]
            scores = windows @ nat
            best = lo + int(np.argmax(scores))
        frame = x[best : best + n]
        out[pos_out : pos_out + n] += frame * win
        norm[pos_out : pos_out + n] += win
        prev_end = best + n
        k += 1
    nz = norm > 1e-3
    out[nz] /= norm[nz]
    return out[: int(len(x) / factor)]


def to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()


class Synth(Protocol):
    name: str

    def synth(self, text: str, rate: float) -> np.ndarray: ...


_NUM_RE = re.compile(r"\d")


@dataclass
class StubTTS:
    """Silence with the duration a Russian speaker would need (≈15 chars/s at 1.0×)."""

    name: str = "stub"
    chars_per_second: float = 15.0

    def synth(self, text: str, rate: float) -> np.ndarray:
        seconds = max(0.3, len(text) / self.chars_per_second / rate)
        return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def model_kind(model_path: str | Path) -> str:
    """``cis`` for the MIT ``v5_cis_*`` packages (manual stress), ``ru`` for ``v5_*_ru`` (auto-stress, CC BY-NC-SA)."""
    return "cis" if "cis" in Path(model_path).name.lower() else "ru"


def build_ssml(text: str, rate: float, accentor: Accentor | None = None, *, require_stress: bool = False) -> tuple[str, str]:
    """Prompt text → (SSML for ``apply_tts``, prosody preset).  Pure function (no torch) so it is unit-testable."""
    text = normalize_text(text)
    if accentor is not None:
        text = accentor(text)
    if require_stress:
        require_stress_marks(text)
    preset = rate_hint(min(rate, 1.3))  # Legacy SSML helper; live synthesis requests neutral 1.0 here
    return f'<speak><prosody rate="{preset}">{_escape(text)}</prosody></speak>', preset


class SileroTTS:
    """Real model; import of torch is deferred until first use."""

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL,
        speaker: str | None = None,
        device: str = "cpu",
        threads: int = 2,
        stress: str = "auto",
    ) -> None:
        import torch  # type: ignore[import-not-found]

        torch.set_num_threads(threads)
        self._torch = torch
        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(self.model_path)
        self.kind = model_kind(self.model_path)
        self.name = "silero-v5-cis-base" if self.kind == "cis" else "silero-v5-ru"
        if self.kind != "cis":
            warnings.warn(
                f"{self.model_path.name}: Silero v5 ru models are CC BY-NC-SA 4.0 – model licence is separate from the application code",
                stacklevel=2,
            )
        imp = torch.package.PackageImporter(str(self.model_path))
        self.model = imp.load_pickle("tts_models", "model")
        self.model.to(device)
        self.speaker = speaker or (DEFAULT_SPEAKER_CIS if self.kind == "cis" else DEFAULT_SPEAKER_RU)
        # the ru models stress text themselves; the CIS models need it from us
        self.accentor: Accentor = load_accentor(stress) if self.kind == "cis" else NoStress()
        self.pipeline_version = NORMALIZER_VERSION + ":" + PROCESSING_VERSION + ":" + accentor_fingerprint(self.accentor)
        self._lock = threading.Lock()

    def synth_quality(self, text: str, rate: float, output_rate: int = SOURCE_RATE) -> np.ndarray:
        # Synthesize at native quality and neutral speed. Apply tempo exactly once.
        ssml, _ = build_ssml(text, 1.0, self.accentor, require_stress=self.kind == "cis")
        with self._lock, self._torch.random.fork_rng(devices=[]):
            # The acoustic model samples noise. Make an unchanged phrase reproducible.
            seed=int.from_bytes(hashlib.sha256((self.speaker+ssml).encode()).digest()[:8],'little')
            self._torch.manual_seed(seed)
            audio = self.model.apply_tts(ssml_text=ssml, speaker=self.speaker, sample_rate=SOURCE_RATE)
        x = np.asarray(audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio, dtype=np.float32)
        return process_audio(x, rate, output_rate)

    def synth(self, text: str, rate: float) -> np.ndarray:
        return self.synth_quality(text, rate, SAMPLE_RATE)


def _escape(text: str) -> str:
    return text.replace("&", "и").replace("<", "").replace(">", "")


def load_synth(model_path: str | Path = DEFAULT_MODEL, speaker: str | None = None, stress: str = "auto") -> Synth:
    try:
        return SileroTTS(model_path, speaker, stress=stress)
    except Exception:  # pragma: no cover - torch / model absent
        return StubTTS()


@dataclass
class TtsModule:
    synth: Synth = field(default_factory=StubTTS)
    cache_size: int = 512
    _cache: "OrderedDict[str, tuple[bytes, int]]" = field(default_factory=OrderedDict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    hits: int = 0
    misses: int = 0

    def render(self, text: str, rate: float) -> tuple[bytes, int]:
        key = hashlib.sha1(f"{rate:.2f}|{text}".encode("utf-8"), usedforsecurity=False).hexdigest()  # cache key only
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self.hits += 1
                return self._cache[key]
        x = self.synth.synth(text, rate)
        pcm = to_pcm16(x)
        item = (pcm, int(len(x) * 1000 / SAMPLE_RATE))
        with self._lock:
            self.misses += 1
            self._cache[key] = item
            if len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return item

    def handle(self, op: str, args: dict) -> tuple[str, dict]:
        if op != "synth":
            return "error", {"error": f"unknown op {op}"}
        rate = float(args.get("rate", 1.0))
        if not 0.5 <= rate <= 2.0:
            return "error", {"error": f"rate {rate} out of range"}
        pcm, ms = self.render(str(args["text"]), rate)
        return "ok", {"duration_ms": ms, "sample_rate": SAMPLE_RATE, "format": "pcm16", "bytes": len(pcm), "pcm": pcm, "engine": self.synth.name, "rate": rate}

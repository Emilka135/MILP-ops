"""ASR gateway on top of ``onnx-asr`` (GigaAM v3 e2e CTC/RNN-T, T-one) – batch transcription of a finished utterance.

The orchestrator sends 8 kHz PCM16 (base64) or a text (stub mode).  Streaming partials come from the
Deeray runtime WebSocket when ``ASR_URL`` points at it; this adapter is the self-hosted MIT/Apache fallback.
"""

from __future__ import annotations

import base64
import time
import os
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class OnnxAsrModule:
    model_name: str = "gigaam-v3-e2e-rnnt"
    engine: str = "onnx-asr"
    _model: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        import onnx_asr  # type: ignore[import-not-found]
        import onnxruntime

        onnxruntime.disable_telemetry_events()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = int(os.environ.get("ASR_THREADS", "2"))
        options.inter_op_num_threads = 1
        provider = os.environ.get("ASR_PROVIDER", "CPUExecutionProvider")
        if provider not in onnxruntime.get_available_providers():
            raise RuntimeError(f"ASR provider unavailable: {provider}")
        quantization = os.environ.get("ASR_QUANTIZATION", "int8")
        self._model = onnx_asr.load_model(self.model_name, path=os.environ.get("ASR_MODEL_PATH") or None, quantization=None if quantization == "none" else quantization,
                                        sess_options=options, providers=[provider])
        self._lock = threading.Lock()

    @staticmethod
    def _to_16k(pcm: bytes, rate: int) -> np.ndarray:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if rate == 16000 or len(x) == 0:
            return x
        n = int(len(x) * 16000 / rate)
        return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)

    def handle(self, op: str, args: dict) -> tuple[str, dict]:
        if op != "transcribe":
            return "error", {"error": f"unknown op {op}"}
        if "pcm_b64" not in args:
            return "error", {"error": "real ASR requires audio"}
        pcm = base64.b64decode(args["pcm_b64"], validate=True)
        if len(pcm) % 2 or int(args.get("rate", 8000)) not in (8000, 16000, 24000, 48000):
            return "error", {"error": "invalid PCM16 audio/rate"}
        x = self._to_16k(pcm, int(args.get("rate", 8000)))
        t0 = time.perf_counter()
        with self._lock:
            text = str(self._model.recognize(x, sample_rate=16000))
        dt = time.perf_counter() - t0
        audio_s = len(x) / 16000 or 1e-6
        return "ok", {"text": text, "final": True, "engine": f"{self.engine}:{self.model_name}", "rtf": round(dt / audio_s, 3)}

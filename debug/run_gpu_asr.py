#!/usr/bin/env python3
"""Verify real WAV inference on CUDA; run inside the GPU ASR image.

ASR_PROVIDER=CUDAExecutionProvider ASR_MODEL_PATH=/models ASR_PROFILE_DIR=/debug \\
  PYTHONPATH=runtime/src python debug/run_gpu_asr.py --wav /debug/input.wav --out /debug/asr.json
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime' / 'src'))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wav', type=Path, required=True)
    parser.add_argument('--out', type=Path, default=ROOT / 'debug/runs/local-gpu/asr.json')
    args = parser.parse_args()
    if os.environ.get('ASR_PROVIDER') != 'CUDAExecutionProvider':
        raise SystemExit('Set ASR_PROVIDER=CUDAExecutionProvider; CPU acceptance is not allowed here')
    with wave.open(str(args.wav), 'rb') as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit('Input must be a real mono PCM16 speech WAV')
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0
    if rms < 20:
        raise SystemExit('Empty/silent WAV cannot prove speech inference')
    from calls.asr.onnx_gateway import OnnxAsrModule
    import onnxruntime

    module = None
    try:
        module = OnnxAsrModule()
        status, result = module.handle('transcribe', {'pcm_b64': base64.b64encode(pcm).decode(), 'rate': rate})
        passed = status == 'ok' and module.cuda_verified and bool(result.get('text', '').strip())
        report = {'passed': passed, 'status': status, 'result': result}
    except Exception as exc:
        report = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
    report.update({
        'onnxruntime_version': onnxruntime.__version__, 'input_wav': str(args.wav),
        'input_sha256': hashlib.sha256(args.wav.read_bytes()).hexdigest(), 'rms_pcm16': rms,
        'seconds': len(samples) / rate, 'preprocessing': 'CPU NumPy FFT; neural model sessions CUDA',
        'session_provider_options': {name: session.get_provider_options() for name, session in module._sessions.items()} if module else {},
        'profiles': module.execution_profiles if module else {},
    })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

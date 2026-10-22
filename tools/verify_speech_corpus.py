#!/usr/bin/env python3
"""Verify fixed caller audio; optional real local ASR adds an independent report."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import wave
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/src'))


def verify(folder, transcribe=None):
    from calls.tts.quality import compare
    folder = Path(folder)
    manifest = json.loads((folder/'manifest.json').read_text())
    spec = json.loads((folder/'spec.json').read_text())
    if hashlib.sha256((folder/'spec.json').read_bytes()).hexdigest() != manifest['spec_sha256']:
        raise ValueError('Frozen corpus specification changed; version it deliberately')
    if len(spec['items']) != len(manifest['items']):
        raise ValueError('Incomplete corpus')
    result = {'method': 'actual_asr_and_signal' if transcribe else 'fixed_bytes_and_signal',
              'naturalness': 'requires human listening', 'items': []}
    for expected, item in zip(spec['items'], manifest['items']):
        if any(item[key] != value for key, value in expected.items()):
            raise ValueError('Manifest differs from frozen specification')
        raw = (folder/item['file']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('Changed recording: '+item['id'])
        with wave.open(io.BytesIO(raw)) as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1,2,8000):
                raise ValueError('Invalid WAV format')
            pcm = audio.readframes(audio.getnframes())
        samples = np.frombuffer(pcm, dtype='<i2').astype(float)
        rms = float(np.sqrt(np.mean(samples*samples)))
        clipped = float(np.mean(np.abs(samples) >= 32760))
        entry = {'id': item['id'], 'rms': rms, 'clipped_fraction': clipped,
                 'passed': len(samples) >= 800 and len(samples) <= 120*8000 and rms > 100 and clipped < .001}
        if transcribe:
            actual = transcribe(pcm)
            score = compare(item['text'], actual)
            entry.update(actual_asr=actual, assessment=score, passed=entry['passed'] and score['passed'])
        result['items'].append(entry)
    result['passed'] = all(item['passed'] for item in result['items'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default=str(ROOT/'tests/fixtures/speech_regression'))
    parser.add_argument('--real-asr', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    transcribe = None
    if args.real_asr:
        import base64
        from calls.asr.onnx_gateway import OnnxAsrModule
        model = OnnxAsrModule()
        def transcribe(pcm):
            status, result = model.handle('transcribe', {'pcm_b64': base64.b64encode(pcm).decode(), 'rate': 8000})
            if status != 'ok':
                raise ValueError(result)
            return result['text']
    result = verify(args.folder, transcribe)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'passed': result['passed'], 'count': len(result['items']), 'method': result['method']}))
    raise SystemExit(0 if result['passed'] else 1)

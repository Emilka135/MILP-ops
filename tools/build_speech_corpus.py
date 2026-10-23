#!/usr/bin/env python3
"""One deliberate offline generation; CI verifies checked-in bytes, never rerenders."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/src'))


def generate(folder, model):
    from calls.tts.silero import SileroTTS, to_pcm16
    folder = Path(folder)
    spec_bytes = (folder / 'spec.json').read_bytes()
    spec = json.loads(spec_bytes)
    if (folder / 'manifest.json').exists() or any(folder.glob('*.wav')):
        raise ValueError('Corpus already exists. Freeze it; a deliberate new version needs a new folder.')
    model_hash = hashlib.file_digest(Path(model).open('rb'), 'sha256').hexdigest()
    synth = SileroTTS(model, stress='context')
    manifest = {'schema_version': 1, 'seed': spec['seed'], 'synthetic': True,
                'spec_sha256': hashlib.sha256(spec_bytes).hexdigest(),
                'model': synth.name, 'model_sha256': model_hash,
                'stress_package': 'silero-stress', 'stress_version': importlib.metadata.version('silero-stress'),
                'pipeline_version': synth.pipeline_version, 'items': []}
    for item in spec['items']:
        if item['voice'] not in synth.model.speakers:
            raise ValueError('Unavailable real voice: '+item['voice'])
        synth.speaker = item['voice']
        pcm = to_pcm16(synth.synth(item['text'], item['speed']))
        path = folder / item['file']
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
            output.writeframes(pcm)
        manifest['items'].append({**item, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                  'duration_ms': len(pcm)*1000//16000})
        print(item['id'], item['voice'], item['speed'], flush=True)
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default=str(ROOT / 'tests/fixtures/speech_regression'))
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    generate(args.folder, args.model)

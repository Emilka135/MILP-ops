"""Read-only audio-bank view; replacement goes through the sole TTS owner."""
import json
import os
from pathlib import Path
import re


def folder(root):
    return Path(os.environ.get('AUDIO_BANK_DIR', str(Path(root)/'data/audio/prompts')))


def manifest(root):
    path=folder(root)/'manifest.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def listing(root):
    return {'prompts':[{'key':key,**entry} for key,entry in manifest(root).items() if entry.get('model_sha256')],
            'format':'WAV / PCM16 mono 8000 Hz'}


def recording(root, key):
    if not re.fullmatch('[0-9a-f]{64}',key) or key not in manifest(root):
        raise ValueError('Запись не найдена')
    return (folder(root)/(key+'.wav')).read_bytes()

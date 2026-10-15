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


def active_manifest(root):
    current=folder(root)/'current.json'
    voice=json.loads(current.read_text()).get('voice') if current.is_file() else None
    return {key:entry for key,entry in manifest(root).items() if not voice or entry.get('voice')==voice}


def listing(root):
    audit_path=folder(root)/'audit.json'
    audit=json.loads(audit_path.read_text()) if audit_path.is_file() else None
    return {'audit':audit,'prompts':[{'key':key,**entry} for key,entry in active_manifest(root).items() if entry.get('model_sha256')],
            'format':'WAV / PCM16 mono 8000 Hz'}


def recording(root, key):
    if not re.fullmatch('[0-9a-f]{64}',key) or key not in manifest(root):
        raise ValueError('Запись не найдена')
    return (folder(root)/(key+'.wav')).read_bytes()


def preview(data):
    from .admin_rpc import module_call
    text=str(data.get('text','')).strip()
    if not text or len(text)>1500:raise ValueError('Введите фразу до 1500 символов')
    text=re.sub(r'\{[^{}]+\}', 'данные из записи', text)
    rate=float(data.get('rate',1.5))
    if not 1.3<=rate<=1.75:raise ValueError('Недопустимая скорость речи')
    return module_call('tts','preview',{'text':text,'rate':rate})

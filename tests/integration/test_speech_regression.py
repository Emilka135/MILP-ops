"""Fixed audible regression fixtures and runtime speech boundaries (no live LLM/MIS)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from calls.tts.processing import process_audio
from calls.tts.silero import TtsModule, StubTTS
from calls.tts.stress import ContextAccentor, load_accentor
from clinic.audio_bank import PromptBank

ROOT = Path(__file__).resolve().parents[2]


def verifier():
    spec = importlib.util.spec_from_file_location('speech_corpus_verify', ROOT/'tools/verify_speech_corpus.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify


def test_frozen_corpus_has_real_audio_voices_and_extreme_speeds():
    folder = ROOT/'tests/fixtures/speech_regression'
    report = verifier()(folder)
    assert report['passed'] and len(report['items']) == 18
    manifest = json.loads((folder/'manifest.json').read_text())
    assert len({item['voice'] for item in manifest['items']}) >= 4
    assert {.75, 2.5} <= {item['speed'] for item in manifest['items']}
    assert manifest['stress_version'] == '1.5' and manifest['model_sha256']


@pytest.mark.parametrize('speed', [.75, 2.5])
def test_extreme_rates_preserve_duration_and_pitch(speed):
    time = np.arange(48000*2)/48000
    source = (.5*np.sin(2*np.pi*440*time)).astype('float32')
    result = process_audio(source, speed)
    assert abs(len(result)/8000 - 2/speed) < .08
    spectrum = np.abs(np.fft.rfft(result*np.hanning(len(result))))
    assert abs(np.fft.rfftfreq(len(result), 1/8000)[spectrum.argmax()] - 440) < 4
    assert TtsModule(StubTTS()).handle('synth', {'text': 'Да', 'rate': speed})[0] == 'ok'


def test_context_mode_passes_whole_sentence_and_explicit_marks(monkeypatch):
    calls = []
    monkeypatch.setattr(ContextAccentor, '__init__', lambda self: setattr(self, '_accentor', lambda text, **kwargs: calls.append((text, kwargs)) or text))
    text = 'Врач Сем+ёнов открыл замок.'
    assert load_accentor('context')(text) == text
    assert len(calls) == 1 and calls[0][0] == text


def unit_bank(tmp_path, pipeline='unit-v1'):
    model = tmp_path/'unit.bin'
    model.write_bytes(b'unit-test model; never acoustic evidence')
    synth = SimpleNamespace(model_path=model, name='unit', speaker='unit', pipeline_version=pipeline)
    class Module:
        def __init__(self):
            self.calls=[]
        def render(self, text, rate):
            self.calls.append(text)
            return np.full(800, 200, dtype='<i2').tobytes(), 100
        def handle(self, op, args):
            pcm, ms=self.render(args['text'], args['rate'])
            return 'ok', {'pcm':pcm, 'duration_ms':ms}
    module=Module()
    return PromptBank(tmp_path/'bank', synth, module), module


def test_doctor_snapshot_prerecords_then_reuses_names_in_dynamic_selection(tmp_path):
    bank, module = unit_bank(tmp_path)
    doctors = [{'id': 7, 'full_name':'Семёнов Алексей Петрович', 'is_active':True}]
    first = bank.refresh_doctors(doctors)
    assert module.calls == ['Семёнов Алексей Петрович']
    assert bank.refresh_doctors(doctors)['generated'] == 1
    status, result = bank.render('Вы выбрали врача Семёнов Алексей Петрович. Верно?', 1.5)
    assert status == 'ok' and result['audio_source'] == 'doctor_bank_composite'
    assert module.calls.count('Семёнов Алексей Петрович') == 1
    assert result['directory_sha256'] == first['directory_sha256']
    bank.refresh_doctors([{'id':7,'full_name':'Семёнов Сергей Петрович'}])
    assert 'Семёнов Сергей Петрович' in module.calls
    assert bank.doctors['directory_sha256'] != first['directory_sha256']
    bank.refresh_doctors([])
    assert bank.render(doctors[0]['full_name']+' Выбран.',1.5)[1]['audio_source'] == 'dynamic_tts'


def test_failed_doctor_refresh_keeps_last_complete_snapshot(tmp_path):
    bank, module = unit_bank(tmp_path)
    bank.refresh_doctors([{'id':1,'full_name':'Иванов Иван Петрович'}])
    previous = bank.doctors.copy()
    module.render = lambda *args: (_ for _ in ()).throw(RuntimeError('TTS unavailable'))
    with pytest.raises(RuntimeError):
        bank.refresh_doctors([{'id':1,'full_name':'Иванов Иван Сергеевич'}])
    assert bank.doctors == previous
    reloaded, _ = unit_bank(tmp_path, pipeline='changed pronunciation')
    assert reloaded.render('Выбран Иванов Иван Петрович.', 1.5)[1]['audio_source'] == 'dynamic_tts'

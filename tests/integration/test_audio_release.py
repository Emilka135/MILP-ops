"""Integrity/revision gates use a certificate fixture; actual ASR runs live in debug."""
import hashlib
import json
from pathlib import Path
import pytest
from clinic.audio_release import require_audio_audit

PROJECT={'flows':[{'rate':1.5,'states':[{'statements':[{'kind':'say','text':'Отправим СМС.','args':{}}]}]}]}


def bank(tmp_path,monkeypatch):
    monkeypatch.setenv('AUDIO_BANK_DIR',str(tmp_path))
    key='a'*64;raw=b'explicit integrity fixture, not speech acceptance'
    (tmp_path/(key+'.wav')).write_bytes(raw)
    (tmp_path/'current.json').write_text(json.dumps({'voice':'model+pipeline'}))
    (tmp_path/'manifest.json').write_text(json.dumps({key:{'text':'Отправим СМС.','rate':1.5,'voice':'model+pipeline','file':key+'.wav'}}))
    report={'status':'passed','voice':'model+pipeline','count':1,'phrases':[{'key':key,'status':'passed','text':'Отправим СМС.','rate':1.5,'file_sha256':hashlib.sha256(raw).hexdigest()}]}
    (tmp_path/'audit.json').write_text(json.dumps(report))
    return key,report


def test_missing_failed_and_changed_recordings_block_release(tmp_path,monkeypatch):
    monkeypatch.setenv('AUDIO_BANK_DIR',str(tmp_path))
    with pytest.raises(ValueError,match='Сначала проверьте'):require_audio_audit(PROJECT,tmp_path)
    key,report=bank(tmp_path,monkeypatch)
    assert require_audio_audit(PROJECT,tmp_path)['count']==1
    (tmp_path/(key+'.wav')).write_bytes(b'changed recording')
    with pytest.raises(ValueError,match='изменена'):require_audio_audit(PROJECT,tmp_path)
    bank(tmp_path,monkeypatch);report['status']='needs_review';(tmp_path/'audit.json').write_text(json.dumps(report))
    with pytest.raises(ValueError,match='не прошёл'):require_audio_audit(PROJECT,tmp_path)


def test_new_text_or_changed_speed_requires_a_new_check(tmp_path,monkeypatch):
    bank(tmp_path,monkeypatch)
    changed=json.loads(json.dumps(PROJECT));changed['flows'][0]['states'][0]['statements'][0]['text']='Отправим эс эм эс завтра.'
    with pytest.raises(ValueError,match='не проверена'):require_audio_audit(changed,tmp_path)
    changed=json.loads(json.dumps(PROJECT));changed['flows'][0]['rate']=1.3
    with pytest.raises(ValueError,match='не проверена'):require_audio_audit(changed,tmp_path)


def test_model_or_processing_change_invalidates_certificate(tmp_path,monkeypatch):
    bank(tmp_path,monkeypatch)
    (tmp_path/'current.json').write_text(json.dumps({'voice':'new-processing'}))
    with pytest.raises(ValueError,match='обработка звука изменились'):require_audio_audit(PROJECT,tmp_path)

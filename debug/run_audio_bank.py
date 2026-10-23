"""Real Silero acceptance: static WAV reuse bypasses synthesis; dynamic names stay out of bank."""
import json
from pathlib import Path
import shutil
import sys
import wave
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'runtime/src')]
from clinic.modules import Speech
from clinic.audio_bank import PromptBank


def main():
    out=ROOT/'debug/runs/audio-bank';out.mkdir(parents=True,exist_ok=True)
    speech=Speech('tts');bank=speech.bank
    key, entry=next((k,e) for k,e in bank.manifest.items() if e.get('model_sha256')==bank.model_sha256)
    render=speech.module.render
    def forbidden(*args,**kwargs):
        raise AssertionError('prerecorded prompt invoked synthesizer')
    speech.module.render=forbidden
    try:
        status,result=bank.render(entry['text'],entry['rate'])
        reloaded=PromptBank(bank.folder, bank.synth, speech.module)
        status2,result2=reloaded.render(entry['text'],entry['rate'])
    finally:
        speech.module.render=render
    assert status==status2=='ok' and result['pcm']==result2['pcm']
    samples=np.frombuffer(result['pcm'],dtype='<i2').astype(np.float32)
    assert len(samples)>800 and float(np.sqrt(np.mean(samples*samples)))>50
    shutil.copy2(bank.folder/entry['file'],out/'01-prerecorded-greeting.wav')
    # Replace with the same real recording to verify the upload and backup path.
    uploaded=bank.replace_recording(key, (bank.folder/entry['file']).read_bytes())
    assert uploaded['recording_type']=='admin_upload'
    assert list((bank.folder/'history').glob(key+'-*.wav'))
    before=len(bank.manifest)
    status,dynamic=bank.render('Проверка: О Коннор записан на шестнадцатое октября, восемь часов утра.',1.5)
    assert status=='ok' and dynamic['audio_source']=='dynamic_tts' and len(bank.manifest)==before
    with wave.open(str(out/'02-dynamic-appointment.wav'),'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000);wav.writeframes(dynamic['pcm'])
    report={'status':'passed','model_sha256':bank.model_sha256,'static_prompt_count':sum(e.get('model_sha256')==bank.model_sha256 for e in bank.manifest.values()),'total_cached_versions':before,'prerecorded_key':key,'cache_bypassed_synthesizer':True,'survived_bank_reload':True,'rms':float(np.sqrt(np.mean(samples*samples))),'dynamic_not_persisted':True,'audio_source':result['audio_source']}
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

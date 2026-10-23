"""Real MIT TTS -> HQ/telephone WAV -> real ASR. No silent/text ASR substitute."""
import argparse
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'runtime/src')]
os.environ.setdefault('ASR_MODEL_PATH',str(ROOT/'modules/asr/models'))
os.environ.setdefault('HF_HUB_OFFLINE','1')
import numpy as np
from calls.tts.silero import SileroTTS, to_pcm16
from calls.tts.normalize import normalize_text
from calls.tts.processing import process_audio
from calls.asr.onnx_gateway import OnnxAsrModule
from calls.scenario.dsl import load_flows

CASES=[
 ('01-sms','Мы отправим СМС с подробностями записи.'),
 ('02-date-time','Вы записаны 16.10.2026 в 14:30, кабинет 201.'),
 ('03-buttons','Подтвердите: нажмите 1. Отказ: 0. Регистратор: 2.'),
 ('04-iso-date','Ваша запись на 2026-11-21 в 09:05.'),
 ('05-identifiers','Номер карты 0012345. Телефон +7 (903) 555-12-34.'),
 ('06-procedures','Возьмите полис ОМС. У вас запись на УЗИ и ЭКГ.'),
]


from calls.tts.quality import compare


def wav(path,pcm,rate):
    with wave.open(str(path),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes(pcm)


def main():
    p=argparse.ArgumentParser();p.add_argument('--all-phrases',action='store_true');p.add_argument('--speaker',default='ru_ekaterina');p.add_argument('--out',default='debug/runs/tts-quality');p.add_argument('--model',default='modules/tts/models/v5_cis_base.pt');a=p.parse_args()
    out=ROOT/a.out;out.mkdir(parents=True,exist_ok=True)
    synth=SileroTTS(ROOT/a.model,speaker=a.speaker,stress='dict');asr=OnnxAsrModule()
    cases=[(n,t,1.5) for n,t in CASES]
    if a.all_phrases:
        seen=set()
        for flow in load_flows(ROOT/'scenarios').values():
            for state in flow.states.values():
                for statement in state.statements:
                    if statement.kind=='say' and '{' not in statement.text:
                        rate=float(statement.args.get('rate',flow.rate));key=(statement.text,rate)
                        if key not in seen:cases.append((flow.name+'-'+state.name,statement.text,rate));seen.add(key)
    report={'status':'running','model':a.model,'model_sha256':hashlib.file_digest((ROOT/a.model).open('rb'),'sha256').hexdigest(),'pipeline_version':synth.pipeline_version,'speaker':a.speaker,'real_asr':True,'all_static_phrases':a.all_phrases,'cases':[]}
    for name,text,rate in cases:
        started=time.monotonic();item={'name':name,'text':text,'rate':rate}
        try:
            spoken=normalize_text(text);hq=synth.synth_quality(text,rate);phone=process_audio(hq,1.0,8000)
            pcm=to_pcm16(phone);samples=np.frombuffer(pcm,dtype='<i2').astype(float)
            wav(out/(name+'-48khz.wav'),to_pcm16(hq),48000);wav(out/(name+'-8khz.wav'),pcm,8000)
            status,result=asr.handle('transcribe',{'pcm_b64':base64.b64encode(pcm).decode(),'rate':8000})
            if status!='ok':raise ValueError(result)
            assessment=compare(spoken,result['text']);rms=float(np.sqrt(np.mean(samples*samples)));clipped=float(np.mean(np.abs(samples)>=32760))
            passed=assessment['passed'] and rms>100 and clipped<.001
            item.update(status='passed' if passed else 'needs_review',spoken_text=spoken,actual_asr=result['text'],rms=round(rms,2),clipped_fraction=clipped,duration_s=round(len(samples)/8000,3),pcm_sha256=hashlib.sha256(pcm).hexdigest(),**assessment)
        except Exception as e:item.update(status='failed',error=str(e))
        item['elapsed_s']=round(time.monotonic()-started,2);report['cases'].append(item)
        report['status']='passed' if all(x['status']=='passed' for x in report['cases']) else 'needs_review'
        (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(name,item['status'],item.get('actual_asr',item.get('error')),flush=True)
    report['total']=len(cases);report['completed']=len(report['cases']);report['failed']=sum(x['status']!='passed' for x in report['cases'])
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=False),flush=True)
    return int(report['status']!='passed')

if __name__=='__main__':raise SystemExit(main())

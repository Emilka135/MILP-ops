#!/usr/bin/env python3
"""Real local doctor-bank/cache and context accent smoke evidence; no external I/O."""
import argparse
import json
from pathlib import Path
import sys
import wave
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime/src'))
from calls.tts.silero import SileroTTS, TtsModule, to_pcm16
from clinic.audio_bank import PromptBank


def main(model, folder):
    output=Path(folder);output.mkdir(parents=True,exist_ok=True)
    synth=SileroTTS(model,stress='context')
    bank=PromptBank(output/'doctors',synth,TtsModule(synth))
    names=[{'id':1,'full_name':'Семёнов Алексей Петрович'}, {'id':2,'full_name':'Иванов Иван Сергеевич'}]
    initial=bank.refresh_doctors(names)
    again=bank.refresh_doctors(names)
    assert initial['generated']==again['generated']
    text='Вы выбрали врача Семёнов Алексей Петрович. Верно?'
    status,result=bank.render(text,1.5)
    assert status=='ok' and result['audio_source']=='doctor_bank_composite'
    def write(name,pcm,rate):
        with wave.open(str(output/name),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate);wav.writeframes(pcm)
    write('doctor-selection-phone.wav',result['pcm'],8000)
    write('doctor-selection-native.wav',to_pcm16(synth.synth_quality(text,1.0)),48000)
    changed=bank.refresh_doctors([{'id':1,'full_name':'Семёнов Сергей Петрович'}])
    assert changed['directory_sha256']!=initial['directory_sha256']
    bank.refresh_doctors([])
    assert not bank.doctors['entries']
    samples=['Врач Семёнов Алексей Петрович.', 'Я уже готов открыть все ваши замки.', 'Фам+илия врача.']
    result={'passed':True,'model':synth.name,'speaker':synth.speaker,'pipeline_version':synth.pipeline_version,
            'initial':initial,'same_snapshot':again,'changed_snapshot':changed,
            'reused_name_in_dynamic_prompt':True,'removed_doctors_inactive':True,
            'stress_samples':[{'text':text,'accented':synth.accentor(text)} for text in samples],
            'naturalness':'requires human listening; not proved by ASR'}
    (output/'runtime.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':True,'generated_names':changed['generated']}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--model',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();main(args.model,args.output)

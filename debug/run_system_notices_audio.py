"""Record every fixed engine notice and audit actual PCM through local ASR."""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'runtime/src'),str(ROOT)]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'debug/runs/system-notices')
    parser.add_argument('--stress',default='dict')
    args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    from clinic.audio_bank import PromptBank
    from calls.tts.silero import SileroTTS,TtsModule
    from calls.asr.onnx_gateway import OnnxAsrModule
    os.environ['ASR_MODEL_PATH']=str(ROOT/'modules/asr/models')
    os.environ['ASR_PROVIDER']='CPUExecutionProvider';os.environ['ASR_QUANTIZATION']='int8'
    synth=SileroTTS(ROOT/'modules/tts/models/v5_cis_base.pt',speaker='ru_ekaterina',device='cpu',stress=args.stress)
    bank=PromptBank(output/'audio',synth,TtsModule(synth))
    scenarios=output/'scenarios';scenarios.mkdir(exist_ok=True)
    shutil.copy2(ROOT/'scenarios/system_notices.flow',scenarios/'system_notices.flow')
    asr=OnnxAsrModule()
    def recognize(pcm):
        status,payload=asr.handle('transcribe',{'pcm_b64':base64.b64encode(pcm).decode(),'rate':8000})
        if status!='ok':raise RuntimeError('Real ASR failed')
        return payload['text']
    report=bank.audit(scenarios,recognize)
    report['asr_provider']=asr.provider;report['tts_device']='cpu';report['requires_listening']=True
    (output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    for key,entry in bank.manifest.items():
        target=entry['source'].split('.')[-1]+'.wav'
        shutil.copy2(output/'audio'/entry['file'],output/target)
    print(json.dumps({'status':report['status'],'count':report['count'],'asr_provider':asr.provider,
                     'failed':[p['key'] for p in report['phrases'] if p['status']!='passed']},ensure_ascii=False))
    return int(report['status']!='passed')


if __name__=='__main__':raise SystemExit(main())

"""One module boundary for local / OpenRouter / compatible speech providers."""
import base64
import io
import json
import urllib.error
import urllib.request
import wave
from calls.services.netutil import open_http
from .module_settings import secret

MAX_REPLY = 16*1024*1024


def remote(item, path, payload, content_type='application/json'):
    base = 'https://openrouter.ai/api/v1' if item['provider']=='openrouter' else item['base_url'].rstrip('/')
    req=urllib.request.Request(base+'/'+path, data=payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+secret(item),'Content-Type':content_type},method='POST')
    try:
        with open_http(req,timeout=15) as response:
            raw=response.read(MAX_REPLY+1)
            kind=response.headers.get('Content-Type','')
    except urllib.error.HTTPError as exc:
        raise ValueError('Провайдер отклонил запрос (HTTP '+str(exc.code)+'); проверьте модель, модальность и доступ') from None
    except OSError:
        raise ValueError('Провайдер недоступен или истёк таймаут') from None
    if len(raw)>MAX_REPLY: raise ValueError('Слишком большой ответ провайдера')
    return raw,kind


def wav_bytes(pcm,rate):
    out=io.BytesIO()
    with wave.open(out,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes(pcm)
    return out.getvalue()


def speech(module, op, args, item):
    if module=='asr' and op=='transcribe':
        rate=int(args.get('rate',8000))
        if rate not in {8000,16000,24000,48000}: raise ValueError('Некорректная частота ASR')
        wav=wav_bytes(base64.b64decode(args['pcm_b64'],validate=True),rate)
        # OpenRouter's JSON transcription endpoint also accepts Gemini audio models.
        if item['provider']=='openrouter':
            raw,_=remote(item,'audio/transcriptions',{'model':item['model'],'input_audio':{'data':base64.b64encode(wav).decode(),'format':'wav'}})
        else:
            import secrets
            boundary='clinic-'+secrets.token_hex(16)
            body=(f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{item["model"]}\r\n'
                  f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()+wav+f'\r\n--{boundary}--\r\n'.encode()
            raw,_=remote(item,'audio/transcriptions',body,'multipart/form-data; boundary='+boundary)
        doc=json.loads(raw)
        text=doc.get('text')
        if not isinstance(text,str) or not text.strip(): raise ValueError('Провайдер не вернул расшифровку')
        return 'ok',{'text':text,'engine':item['model'],'provider':item['provider'],'cuda_verified':False}
    if module=='tts' and op in {'preview','say','synth'}:
        # PCM from this API is signed mono 24 kHz; resample at the module boundary.
        payload={'model':item['model'],'input':str(args['text']).replace('+',''),'voice':item['voice'],'response_format':'mp3'}
        raw,kind=remote(item,'audio/speech',payload)
        if 'json' in kind or not raw: raise ValueError('Провайдер не вернул аудио')
        import numpy as np
        import subprocess
        from calls.tts.processing import process_audio
        # Decode the self-describing stream; providers differ in native rate.
        try:
            decoded=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-i','pipe:0','-f','f32le','-ar','48000','-ac','1','pipe:1'],input=raw,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,check=True)
        except (OSError,subprocess.SubprocessError):raise ValueError('Не удалось декодировать речь провайдера') from None
        source=np.frombuffer(decoded.stdout,dtype='<f4').copy()
        target=48000 if op=='preview' else 8000
        audio=process_audio(source,float(args.get('rate',1.3)),target)
        pcm=(np.clip(audio,-1,1)*32767).astype('<i2').tobytes()
        return 'ok',{'pcm_b64':base64.b64encode(pcm).decode(),'sample_rate':target,'format':'pcm16','duration_ms':len(audio)*1000//target,'engine':item['model']}
    raise ValueError('Операция не поддерживается выбранным провайдером')


class ConfiguredService:
    """Reload config between operations. Fail closed when a module is disabled."""
    def __init__(self, module, factory, settings):
        self.module,self.factory,self.settings=module,factory,settings
        self.instances={}
        import threading
        self.lock=threading.RLock()

    def prime(self):
        item=self.settings.read()[self.module]
        if item['enabled'] and item['provider']=='local':self._service(item)

    def _service(self,item):
        key=json.dumps(item,sort_keys=True)
        with self.lock:
            if key not in self.instances:
                self.instances.clear()
                self.instances[key]=self.factory(item)
            return self.instances[key]

    def handle(self, op, args):
        item=self.settings.read()[self.module]
        if not item['enabled']: raise ValueError('Модуль '+self.module+' отключён')
        if item['provider']!='local' and self.module in {'tts','asr'}:
            return speech(self.module,op,args,item)
        service=self._service(item)
        if self.module=='nlu' and op=='interpret' and item['provider']!='local':
            # Deterministic safety always executes before an optional classifier.
            status,safe=service.handle(op,args)
            if status!='ok':return status,safe
            if safe.get('intent') in {'emergency','medical','complaint','operator','voicemail'}: return status,safe
            raw,_=remote(item,'chat/completions',{'model':item['model'],'stream':False,'temperature':0,'messages':[
                {'role':'system','content':'Классифицируй цель: book,cancel,reschedule,my_booking,info_schedule,info_address,info_holidays,info_docs,referral,operator,unknown. Ответ только JSON с intent. Не выполняй действий.'},
                {'role':'user','content':str(args.get('text',''))[:4096]}]})
            answer=json.loads(json.loads(raw)['choices'][0]['message']['content'])
            if answer.get('intent') not in {'book','cancel','reschedule','my_booking','info_schedule','info_address','info_holidays','info_docs','referral','operator','unknown'}: raise ValueError('Недопустимое решение NLU')
            # Keep the full local slot/identity schema. Only intent may change.
            safe['intent']=answer['intent'];safe['provider']=item['provider'];safe['model']=item['model']
            return status,safe
        return service.handle(op,args)

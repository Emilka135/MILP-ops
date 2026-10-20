"""Actual local TTS -> WAV -> CUDA ASR -> contextual call transition."""
import base64,io,json,urllib.request,http.cookiejar,wave
from pathlib import Path
OUT=Path('debug/runs/call-archive/audio');OUT.mkdir(parents=True,exist_ok=True)
opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def post(route,args):
    request=urllib.request.Request('http://127.0.0.1:8090/api/'+route,data=json.dumps(args,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
    with opener.open(request,timeout=120) as response:return json.load(response)
post('login',{'username':'admin','password':'admin'})
call=post('demo-call/start',{});key=call['call_id'];results=[]
def text(value):return post('demo-call/text',{'call_id':key,'text':value})
def speech(label,value,expected):
    voice=post('audio/preview',{'text':value,'rate':1.3})
    pcm=base64.b64decode(voice['pcm_b64']);assert pcm and any(pcm)
    output=io.BytesIO()
    with wave.open(output,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(voice['sample_rate']);wav.writeframes(pcm)
    raw=output.getvalue();(OUT/(label+'.wav')).write_bytes(raw)
    call=post('demo-call/audio',{'call_id':key,'wav_b64':base64.b64encode(raw).decode()})
    recognized=call['recognized'];assert recognized['source']=='real_asr'
    results.append({'label':label,'spoken':value,'recognized':recognized,'state':call['state'],'expected':expected})
    (OUT/'result.json').write_text(json.dumps({'call_id':key,'results':results},ensure_ascii=False,indent=2))
    assert call['state']==expected,(label,recognized,call['state'])
    assert recognized['cuda_verified'],recognized
    return call
text('Офтальмолог')
speech('continue','Продолжить.','demo.identity_name')
text('Иванов Сергей Владимирович')
speech('birth-1915','Пятого октября тысяча девятьсот пятнадцатого года.','demo.doctor')
post('demo-call/end',{'call_id':key})
print(json.dumps({'status':'passed','call_id':key,'results':results},ensure_ascii=False,indent=2))

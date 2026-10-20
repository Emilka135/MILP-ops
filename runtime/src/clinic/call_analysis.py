"""Replay an imported dialogue without executing a booking or synthesizing turns."""
import base64
import io
import json
import secrets
import wave
from pathlib import Path
from .demo_call import Call, TITLES, TERMINAL


def transcribe(demo, encoded):
    try:
        raw=base64.b64decode(encoded,validate=True)
        if len(raw)>2*1024*1024:raise ValueError('WAV должен быть до 2 MiB')
        with wave.open(io.BytesIO(raw),'rb') as w:
            rate=w.getframerate()
            if w.getnchannels()!=1 or w.getsampwidth()!=2 or rate not in {8000,16000,24000,48000} or w.getcomptype()!='NONE':raise ValueError('Нужен моно WAV PCM16: 8/16/24/48 кГц')
            frames=w.getnframes()
            if not frames or frames>rate*120:raise ValueError('Нужен WAV до двух минут')
            pcm=w.readframes(frames)
            if len(pcm)!=frames*2:raise ValueError('WAV обрезан')
    except (wave.Error,EOFError,TypeError):raise ValueError('Не удалось прочитать WAV') from None
    chunks=[]
    for offset in range(0,len(pcm),rate*2*10):
        result=demo.rpc('asr','transcribe',{'pcm_b64':base64.b64encode(pcm[offset:offset+rate*2*10]).decode(),'rate':rate})
        chunks.append(result.get('text',''))
    return {'text':'\n'.join(chunks),'speaker_status':'unknown','note':'Говорящие не разделены. Разметьте строки «Пациент: …» и «Сотрудник: …» перед анализом.'}


def analyze(demo, data, owner):
    text=data.get('text','')
    if not isinstance(text,str) or not 0<len(text)<=100000:raise ValueError('Передайте расшифровку до 100000 символов')
    turns=[]
    for line in text.splitlines():
        if not line.strip():continue
        role,sep,value=line.partition(':')
        role={'пациент':'patient','клиент':'patient','сотрудник':'assistant','оператор':'assistant','помощник':'assistant'}.get(role.strip().lower())
        if not sep or role is None or not value.strip():raise ValueError('Каждая строка: «Пациент: …» или «Сотрудник: …»')
        turns.append({'role':role,'text':value.strip(),'source':'imported_transcript'})
    if not turns or len(turns)>500:raise ValueError('Нужно от 1 до 500 размеченных реплик')
    key=secrets.token_urlsafe(32)
    call=Call(owner,0,key=key,analysis=True,registrar=str(data.get('_line') or '100'),modules=demo.settings.read())
    mapping=[]
    for turn in turns:
        before=call.state
        turn['state']='demo.'+before
        turn['step_index']=len(call.trace)-1
        call.transcript.append(turn)
        if turn['role']=='patient':
            expected=demo._advance(call,turn['text'],None) if before not in TERMINAL else ''
            matched=call.state!=before
            mapping.append({'turn':len(call.transcript)-1,'from':'demo.'+before,'to':'demo.'+call.state,
                            'status':'matched' if matched else 'needs_review','expected_reply':expected})
    document=demo.document(call)
    if data.get('consent_audio') is True and data.get('wav_b64'):
        # Validate format and bounds without invoking ASR for a second time.
        raw=base64.b64decode(data['wav_b64'],validate=True)
        if len(raw)>2*1024*1024:raise ValueError('Аудиофайл больше 2 MiB')
        try:
            with wave.open(io.BytesIO(raw),'rb') as wav:
                if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getframerate() not in {8000,16000,24000,48000}:raise ValueError('Нужен моно WAV PCM16')
                if wav.getnframes()>wav.getframerate()*120:raise ValueError('WAV длиннее двух минут')
        except (wave.Error,EOFError):raise ValueError('Не удалось прочитать WAV') from None
        folder=demo.store.audio/key;folder.mkdir(exist_ok=True)
        (folder/'import.wav').write_bytes(raw)
        document['consent_audio']=True;document['audio_files']=['import.wav']
    if data.get('upload_id'):
        document['audio_files']=demo.uploads.finish(str(data['upload_id']),owner,key,data.get('consent_audio') is True)
        document['consent_audio']=data.get('consent_audio') is True
    document['mapping']=mapping
    document['analysis_note']='Сопоставление с текущим тестовым сценарием; переходы требуют проверки сотрудником. Реальные записи и направления не подтверждаются.'
    demo.store.save_call(key,owner,document)
    return document


class Uploads:
    """Bounded, expiring upload buffers in container /tmp; consent controls archive."""
    def __init__(self, demo):
        import tempfile,threading
        self.demo=demo;self.items={};self.lock=threading.RLock()
        self.root=Path(tempfile.mkdtemp(prefix='clinic-import-',dir=demo.tmp))

    def request(self, data, owner):
        import time,shutil
        with self.lock:
            for key,item in list(self.items.items()):
                if item['expires']<time.monotonic():shutil.rmtree(self.root/key,ignore_errors=True);self.items.pop(key)
            if data.get('op')=='start':
                if len(self.items)>=4:raise ValueError('Слишком много загружаемых разговоров')
                key=secrets.token_urlsafe(32);(self.root/key).mkdir()
                self.items[key]={'owner':owner,'expires':time.monotonic()+900,'files':[],'seconds':0,'text':[]}
                return {'upload_id':key}
            key=str(data.get('upload_id',''));item=self.items.get(key)
            if not item or item['owner']!=owner:raise ValueError('Загрузка завершена или не найдена')
            if data.get('op')=='cancel':
                shutil.rmtree(self.root/key,ignore_errors=True);self.items.pop(key)
                return {'cancelled':True}
            if data.get('op')!='chunk':raise ValueError('Неизвестное действие загрузки')
            if data.get('index')!=len(item['files']):raise ValueError('Нарушен порядок фрагментов')
            pcm,rate=self.demo._decode(data.get('wav_b64'),allow_silence=True)
            if item['seconds']+len(pcm)/2/rate>600:raise ValueError('Разговор длиннее десяти минут')
            import array,math
            values=array.array('h');values.frombytes(pcm)
            audible=math.sqrt(sum(v*v for v in values)/len(values))>=60
            recognized=self.demo.rpc('asr','transcribe',{'pcm_b64':base64.b64encode(pcm).decode(),'rate':rate}) if audible else {'text':''}
            from .provider_adapter import wav_bytes
            path=self.root/key/(str(len(item['files']))+'.wav');path.write_bytes(wav_bytes(pcm,rate))
            item['files'].append(path);item['seconds']+=len(pcm)/2/rate;item['text'].append(str(recognized.get('text','')))
            item['expires']=time.monotonic()+900
            return {'upload_id':key,'text':'\n'.join(item['text']),'chunks':len(item['files']),'speaker_status':'unknown',
                    'note':'Разметьте строки «Пациент: …» и «Сотрудник: …» перед анализом.'}

    def finish(self, key, owner, call_id, consent):
        import shutil
        with self.lock:
            item=self.items.get(key)
            if not item or item['owner']!=owner:raise ValueError('Загрузка завершена или не найдена')
            files=[]
            if consent:
                folder=self.demo.store.audio/call_id;folder.mkdir(exist_ok=True)
                for i,path in enumerate(item['files']):
                    name=f'import-{i+1}.wav';shutil.copyfile(path,folder/name);files.append(name)
            shutil.rmtree(self.root/key,ignore_errors=True);self.items.pop(key)
            return files

"""Anonymous speech sandbox. The only external operations are ASR and HQ TTS.

No CRM client, database handle, patient identifier or arbitrary module operation is
accepted. A successful confirmation is an explanation, never a medical booking.
"""
from __future__ import annotations

import array
import base64
from dataclasses import dataclass, field
import io
import math
import secrets
import threading
import time
import wave

from .admin_rpc import module_call
from calls.nlu.normalize import parse_yes_no

CATALOG_SOURCE = 'docs/requirements/clinic-regulation.txt, раздел 2.3'
SPECIALISTS = (
    ('cardiology', 'Кардиолог', 'кардиолог', True),
    ('neurology', 'Невролог', 'невролог', True),
    ('surgery', 'Хирург', 'хирург', False),
    ('ophthalmology', 'Офтальмолог', 'офтальмолог', False),
    ('ent', 'Оториноларинголог', 'лор|оториноларинголог', False),
    ('endocrinology', 'Эндокринолог', 'эндокринолог', True),
    ('gastroenterology', 'Гастроэнтеролог', 'гастроэнтеролог', True),
    ('urology', 'Уролог', 'уролог', True),
    ('pulmonology', 'Пульмонолог', 'пульмонолог', True),
)
TITLES = {'menu':'Куда записаться?', 'specialist':'Выбираем специалиста',
          'referral':'Уточняем направление', 'therapist_first':'Сначала участковый врач',
          'confirm':'Проверяем выбранный вариант', 'done':'Пробный маршрут пройден',
          'operator':'Помощь регистратора', 'emergency':'Экстренное обращение', 'closed':'Звонок завершён'}
PROMPTS = {
 'menu':'Здравствуйте! Куда хотите записаться? К участковому врачу — один. К специалисту — два. На исследование или анализы — три. Можно ответить голосом.',
 'specialist':'Назовите специалиста. Для помощи регистратора нажмите ноль.',
 'therapist_first':'Без направления сначала обратитесь к участковому врачу. Выбрать участкового врача — один. Назад — ноль. Регистратор — два.',
 'done':'Пробный разговор завершён. Спасибо!' ,
 'operator':'Здесь нужна помощь регистратора.',
 'emergency':'Если состояние резко ухудшилось, звоните сто три или сто двенадцать.',
}
MAX_SECONDS = 12
MAX_WAV_BYTES = MAX_SECONDS * 48000 * 2 + 4096


@dataclass
class Call:
    owner: str
    expires: float
    state: str = 'menu'
    selection: dict | None = None
    transcript: list = field(default_factory=list)
    trace: list = field(default_factory=lambda: ['menu'])
    attempts: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class DemoCalls:
    def __init__(self, rpc=None, clock=None, limit=32, ttl=900):
        self.rpc = rpc or module_call
        self.clock = clock or time.monotonic
        self.limit, self.ttl = limit, ttl
        self.calls = {}
        self.lock = threading.Lock()
        self.voice_cache = {}

    def request(self, route, post, data, query=None, client=''):
        """Editor integration: this namespace alone permits anonymous POSTs."""
        prefix = '/api/demo-call/'
        if not route.startswith(prefix):
            raise ValueError('Неизвестное действие пробного звонка')
        op = route[len(prefix):]
        if op == 'catalog' and not post:
            return {'source':CATALOG_SOURCE, 'items':[
                {'id':'therapist','label':'Участковый терапевт','referral_required':False},
                {'id':'specialist','label':'Врач-специалист','details':[{'id':v[0],'label':v[1],'referral_required':v[3]} for v in SPECIALISTS]},
                {'id':'diagnostic','label':'ЭКГ, УЗИ, рентгенография, анализы','referral_required':True,
                 'note':'По направлению; платно — без направления. Платную запись этот пример не оформляет.'}]}
        if not post or op not in {'start','audio','dtmf','end'}:
            raise ValueError('Неизвестное действие пробного звонка')
        if op == 'start':
            with self.lock:
                now = self.clock()
                self.calls = {k:v for k,v in self.calls.items() if v.expires > now}
                if len(self.calls) >= self.limit or sum(c.owner==client for c in self.calls.values()) >= 4:
                    raise ValueError('Слишком много пробных звонков. Завершите предыдущий или попробуйте позже.')
                key = secrets.token_urlsafe(32)
                call = Call(client, now+self.ttl)
                self.calls[key] = call
            try:
                with call.lock:
                    return self._result(key,call,PROMPTS['menu'])
            except Exception:
                with self.lock:self.calls.pop(key,None)
                raise
        key = str(data.get('call_id',''))
        with self.lock:
            call = self.calls.get(key)
            if call is None or call.expires <= self.clock() or call.owner != client:
                raise ValueError('Пробный звонок завершён. Начните новый.')
        if not call.lock.acquire(blocking=False):
            raise ValueError('Дождитесь ответа помощника')
        try:
            if op == 'end':
                with self.lock:self.calls.pop(key,None)
                return {'call_id':key,'state':'demo.closed','title':TITLES['closed'],'sandbox':True,'executed':False}
            if call.state in {'done','operator','emergency'}:
                raise ValueError('Пробный звонок завершён. Начните новый.')
            if len(call.transcript) >= 40:
                raise ValueError('Достигнут предел пробного разговора. Начните новый.')
            if op == 'audio':
                pcm,rate = self._decode(data.get('wav_b64'))
                recognized = self.rpc('asr','transcribe',{'pcm_b64':base64.b64encode(pcm).decode(),'rate':rate})
                text = str(recognized.get('text','')).strip()[:1000]
                if not text:raise ValueError('Речь не распознана. Повторите или используйте клавиши.')
                source = {'role':'patient','text':text,'source':'real_asr','engine':recognized.get('engine',''),
                          'provider':recognized.get('provider',''),'cuda_verified':recognized.get('cuda_verified',False),
                          'state':'demo.'+call.state}
                digit = None
            else:
                digit = str(data.get('digit',''))
                if digit not in {'0','1','2','3','*','#'}:raise ValueError('Неизвестная клавиша')
                text = ''
                source = {'role':'dtmf','text':digit,'source':'dtmf','state':'demo.'+call.state}
            call.transcript.append(source)
            prompt = self._advance(call,text,digit)
            call.expires = self.clock()+self.ttl
            result = self._result(key,call,prompt)
            result['recognized'] = source if op == 'audio' else None
            return result
        finally:
            call.lock.release()

    @staticmethod
    def _decode(encoded):
        if not isinstance(encoded,str) or len(encoded) > (MAX_WAV_BYTES+2)//3*4:
            raise ValueError('Запишите не больше двенадцати секунд речи')
        try:
            raw = base64.b64decode(encoded,validate=True)
            if len(raw)>MAX_WAV_BYTES:raise ValueError('Слишком большая запись')
            with wave.open(io.BytesIO(raw),'rb') as wav:
                channels,width,rate,frames = wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes()
                if channels!=1 or width!=2 or wav.getcomptype()!='NONE' or rate not in {8000,16000,24000,48000}:
                    raise ValueError('Нужен моно WAV PCM16 с частотой 8, 16, 24 или 48 кГц')
                if not rate*.15 <= frames <= rate*MAX_SECONDS:
                    raise ValueError('Запишите от одной секунды до двенадцати секунд речи')
                pcm = wav.readframes(frames)
                if len(pcm)!=frames*2:raise ValueError('Запись обрезана')
            values=array.array('h');values.frombytes(pcm)
            import sys
            if sys.byteorder!='little':values.byteswap()
            if math.sqrt(sum(v*v for v in values)/len(values))<60:
                raise ValueError('Не слышно речи. Проверьте микрофон или используйте клавиши.')
            return pcm,rate
        except (wave.Error,EOFError,TypeError,ValueError) as exc:
            if isinstance(exc,ValueError) and str(exc).startswith(('Нужен','Запишите','Запись','Не слышно','Слишком')):raise
            raise ValueError('Не удалось прочитать WAV. Запишите фразу ещё раз.') from None

    @staticmethod
    def _move(call,state):
        call.state=state;call.trace.append(state);call.attempts=0
        if state=='menu':call.selection=None

    def _choose(self,call,item):
        call.selection=item
        self._move(call,'referral' if item['referral_required'] else 'confirm')
        return self._prompt(call)

    def _prompt(self,call):
        if call.state=='referral':
            return 'Есть направление? Да — один. Нет — ноль. Регистратор — два.'
        if call.state=='confirm':
            return 'Выбрано: '+call.selection['label']+'. Продолжить — один. Назад — ноль. Регистратор — два.'
        return PROMPTS[call.state]

    def _advance(self,call,text,digit):
        import re
        low=text.lower().replace('ё','е')
        if digit=='*' or re.search(r'оператор|регистратор|жив[а-я]+\s+человек',low):
            self._move(call,'operator');return PROMPTS['operator']
        if re.search(r'задыха|не могу дышать|потер[а-я]+ сознани|скорая помощь|очень сильн[а-я]* бол',low):
            self._move(call,'emergency');return PROMPTS['emergency']
        if call.state=='specialist' and digit=='0':
            self._move(call,'operator');return PROMPTS['operator']
        if digit=='#':
            self._move(call,'menu');return PROMPTS['menu']
        if call.state in {'referral','confirm','therapist_first'}:
            yes_no=parse_yes_no(text) if text else None
            if call.state=='referral' and re.search(r'направлен',low):
                yes_no=False if re.search(r'\b(?:нет|не|без)\b',low) else (True if re.search(r'\b(?:есть|имеется|получил[аи]?|дали|выдали)\b|на руках',low) else yes_no)
            if digit=='2':
                self._move(call,'operator');return PROMPTS['operator']
            yes=digit=='1' or (digit is None and yes_no is True)
            no=digit=='0' or (digit is None and yes_no is False)
            if yes:
                if call.state=='confirm':self._move(call,'done');return PROMPTS['done']
                if call.state=='therapist_first':
                    child=bool(call.selection and call.selection['label'].startswith('Детский '))
                    return self._choose(call,{'id':'pediatrician' if child else 'therapist','label':'Участковый педиатр' if child else 'Участковый терапевт','referral_required':False})
                self._move(call,'confirm');return self._prompt(call)
            if no:
                self._move(call,'therapist_first' if call.state=='referral' else 'menu');return self._prompt(call)
        else:
            if re.search(r'анализ|исследован|узи|экг|рентген',low) or (call.state=='menu' and digit=='3'):
                return self._choose(call,{'id':'diagnostic','label':'Исследование или лабораторные анализы','referral_required':True})
            if re.search(r'педиатр',low):
                return self._choose(call,{'id':'pediatrician','label':'Участковый педиатр','referral_required':False})
            if re.search(r'терапевт|участков',low) or (call.state=='menu' and digit=='1'):
                child=bool(re.search(r'реб[её]н|ребят|детск|доч|сын',low))
                return self._choose(call,{'id':'pediatrician' if child else 'therapist','label':'Участковый педиатр' if child else 'Участковый терапевт','referral_required':False})
            selected=next((item for item in SPECIALISTS if re.search(item[2],low)),None)
            if selected:
                child=bool(re.search(r'реб[её]н|ребят|детск|доч|сын',low))
                label=('Детский '+selected[1].lower()) if child and selected[0] in {'cardiology','neurology','surgery','ophthalmology','ent','endocrinology'} else selected[1]
                return self._choose(call,{'id':selected[0],'label':label,'referral_required':selected[3]})
            if (call.state=='menu' and digit=='2') or re.search(r'специалист|врач',low):
                self._move(call,'specialist');return PROMPTS['specialist']
        call.attempts+=1
        if call.attempts>=3:
            self._move(call,'operator');return PROMPTS['operator']
        return 'Не получилось уверенно выбрать ответ. Повторите, пожалуйста, или нажмите подходящую клавишу.'

    def _result(self,key,call,prompt):
        with self.lock:voice=self.voice_cache.get(prompt)
        if voice is None:
            voice=self.rpc('tts','preview',{'text':prompt,'rate':1.3})
            if voice.get('sample_rate')!=48000 or not voice.get('pcm_b64'):
                raise ValueError('Модуль не вернул речь в качестве сорок восемь килогерц')
            with self.lock:self.voice_cache[prompt]=voice
        call.transcript.append({'role':'assistant','text':prompt,'state':'demo.'+call.state,'source':'hq_tts'})
        return {'call_id':key,'sandbox':True,'executed':False,'state':'demo.'+call.state,
                'title':TITLES[call.state],'steps':[{'id':'demo.'+s,'title':TITLES[s]} for s in call.trace],'selection':call.selection,'prompt':prompt,'audio':voice,
                'transcript':list(call.transcript),'trace':['demo.'+s for s in call.trace],
                'proposed_action': 'В реальной системе: проверить личность, направление и свободное время' if call.state=='confirm' else ('Пробный выбор подтверждён; изменений в МИС нет' if call.state=='done' else 'Выбрать следующий шаг'),
                'catalog_source':CATALOG_SOURCE,'finished':call.state in {'done','operator','emergency'}}


def speech_phrases():
    """Finite demo vocabulary for stress preflight; no patient-dependent speech."""
    demo = DemoCalls()
    phrases = set(PROMPTS.values())
    phrases.add('Не получилось уверенно выбрать ответ. Повторите, пожалуйста, или нажмите подходящую клавишу.')
    phrases.add(demo._prompt(Call('', 0, state='referral')))
    labels = ['Участковый терапевт', 'Участковый педиатр', 'Исследование или лабораторные анализы']
    labels += [v[1] for v in SPECIALISTS]
    labels += ['Детский '+v[1].lower() for v in SPECIALISTS if v[0] in {'cardiology','neurology','surgery','ophthalmology','ent','endocrinology'}]
    for label in labels:
        phrases.add(demo._prompt(Call('', 0, state='confirm', selection={'label':label})))
    return sorted(phrases)

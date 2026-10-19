"""Test calls run a real, inspectable booking protocol against an isolated stand."""
from __future__ import annotations
import array
import base64
from dataclasses import dataclass, field
from datetime import date, datetime
import io
import math
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import wave

from .admin_rpc import module_call
from .module_settings import configured
from .sandbox_store import SandboxStore
from calls.nlu.normalize import parse_yes_no

CATALOG_SOURCE = 'docs/requirements/clinic-regulation.txt, раздел 2.3'
POLICY_VERSION = 'sandbox-booking-2'
SPECIALISTS = (
 ('cardiology','Кардиолог','кардиолог',True), ('neurology','Невролог','невролог',True),
 ('surgery','Хирург','хирург',False), ('ophthalmology','Офтальмолог','офтальмолог|окулист|глазни[кцч]',False),
 ('ent','Оториноларинголог',r'\bлор\b|оториноларинголог|ухо.?горло',False),
 ('endocrinology','Эндокринолог','эндокринолог',True), ('gastroenterology','Гастроэнтеролог','гастроэнтеролог',True),
 ('urology','Уролог','уролог',True), ('pulmonology','Пульмонолог','пульмонолог',True),
)
TITLES = dict(menu='Куда записаться?',specialist='Выбираем специалиста',referral='Уточняем направление',
 therapist_first='Сначала участковый врач',confirm='Проверяем выбранный вариант',identity_name='Фамилия, имя и отчество',
 identity_birth='Дата рождения',slots='Свободные талоны',booking_confirm='Подтверждение записи',
 done='Запись в тестовой таблице',operator='Помощь регистратора',emergency='Экстренное обращение',closed='Звонок завершён')
PROMPTS = {
 'menu':'Здравствуйте! Куда хотите записаться? К участковому врачу — один. К специалисту — два. На исследование или анализы — три. Можно ответить голосом.',
 'specialist':'Назовите специалиста. Для помощи регистратора нажмите ноль.',
 'referral':'Есть направление? Да — один. Нет — ноль. Регистратор — два.',
 'therapist_first':'Без направления сначала обратитесь к участковому врачу. Выбрать участкового врача — один. Назад — ноль. Регистратор — два.',
 'identity_name':'Назовите фамилию, имя и отчество полностью. Это тестовая картотека: Иванов Иван Иванович или Петрова Анна Сергеевна.',
 'identity_birth':'Назовите дату рождения: день, месяц и год. Для тестового Иванова: двенадцатое апреля тысяча девятьсот восемьдесят пятого года.',
 'operator':'Перевожу на оператора. В тестовом режиме передача отмечена в журнале звонка.',
 'emergency':'Если состояние резко ухудшилось, звоните сто три или сто двенадцать. Требуется помощь сотрудника.',
 'done':'Запись сохранена в тестовой таблице. В настоящей поликлинике запись не создавалась. Спасибо!',
}
TERMINAL = {'done','operator','emergency','closed'}
BRANCHES = {
 'menu':['specialist','referral','confirm','operator','emergency'], 'specialist':['referral','confirm','operator'],
 'referral':['confirm','therapist_first','operator'], 'therapist_first':['confirm','menu','operator'],
 'confirm':['identity_name','menu','operator'], 'identity_name':['identity_birth','operator'],
 'identity_birth':['slots','operator'], 'slots':['booking_confirm','operator'], 'booking_confirm':['done','slots','operator'],
}
MAX_SECONDS = 12
MAX_WAV_BYTES = MAX_SECONDS*48000*2+4096


@dataclass
class Call:
    owner: str
    expires: float
    state: str = 'menu'
    selection: dict | None = None
    transcript: list = field(default_factory=list)
    trace: list = field(default_factory=lambda:['menu'])
    attempts: int = 0
    consent: bool = False
    flags: list = field(default_factory=list)
    patient_name: str = ''
    patient: dict | None = None
    offers: list = field(default_factory=list)
    slot: dict | None = None
    booking: dict | None = None
    queries: list = field(default_factory=list)
    key: str = ''
    analysis: bool = False
    modules: dict = field(default_factory=dict)
    extensions: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class DemoCalls:
    def __init__(self,rpc=None,clock=None,limit=32,ttl=900,store=None,settings=None,tmp=None):
        self.rpc=rpc or module_call
        self.clock=clock or time.monotonic
        self.limit,self.ttl=limit,ttl
        self.calls={};self.lock=threading.Lock();self.voice_cache={}
        self._temporary = tempfile.TemporaryDirectory(prefix='clinic-unit-') if store is None else None
        self.store=store or SandboxStore(self._temporary.name)
        self.settings=settings or configured()
        self.tmp=Path(tmp or tempfile.gettempdir())/'clinic-call-tmp'
        self.tmp.mkdir(parents=True,exist_ok=True)

    def request(self,route,post,data,query=None,client=''):
        prefix='/api/demo-call/'
        if not route.startswith(prefix):raise ValueError('Неизвестное действие пробного звонка')
        op=route[len(prefix):]
        if op=='catalog' and not post:
            return {'source':CATALOG_SOURCE,'items':[{'id':'therapist','label':'Участковый терапевт'},
                {'id':'specialist','label':'Врач-специалист','details':[{'id':v[0],'label':v[1],'referral_required':v[3]} for v in SPECIALISTS]}]}
        if not post or op not in {'start','audio','text','dtmf','end','inspect'}:raise ValueError('Неизвестное действие пробного звонка')
        if op=='start':
            if type(data.get('consent_audio',False)) is not bool:raise ValueError('Укажите согласие на аудиозапись')
            with self.lock:
                now=self.clock()
                expired=[k for k,v in self.calls.items() if v.expires<=now]
                for k in expired:self.calls.pop(k)
                if len(self.calls)>=self.limit or sum(c.owner==client for c in self.calls.values())>=4:raise ValueError('Слишком много пробных звонков. Завершите предыдущий.')
                key=secrets.token_urlsafe(32)
                call=Call(client,now+self.ttl,consent=data.get('consent_audio',False),key=key,modules=self.settings.read())
                if call.modules['scenario_extensions']['enabled']:
                    from .scenario_extensions import snapshot
                    call.extensions=snapshot(self.store,data.get('_line','100'))
                self.calls[key]=call
            try:
                with call.lock:return self._result(key,call,PROMPTS['menu'])
            except Exception:
                with self.lock:self.calls.pop(key,None)
                raise
        key=str(data.get('call_id',''))
        with self.lock:
            call=self.calls.get(key)
            if call is None or call.expires<=self.clock() or call.owner!=client:raise ValueError('Пробный звонок завершён. Начните новый.')
        if not call.lock.acquire(blocking=False):raise ValueError('Дождитесь ответа помощника')
        try:
            if op=='inspect':return self.document(call)
            if op=='end':
                self._persist(call)
                with self.lock:self.calls.pop(key,None)
                return {'call_id':key,'state':'demo.closed','title':TITLES['closed'],'sandbox':True,'executed':False,'archived':True}
            if call.state in TERMINAL:raise ValueError('Пробный звонок завершён. Начните новый.')
            if len(call.transcript)>=120:raise ValueError('Достигнут предел пробного разговора')
            digit=None
            if op=='audio':
                if not self.enabled(call,'asr'):raise ValueError('Распознавание речи отключено')
                pcm,rate=self._decode(data.get('wav_b64'))
                # Transient audio is always removed, including provider failures.
                with tempfile.NamedTemporaryFile(dir=self.tmp,suffix='.wav') as temp:
                    from .provider_adapter import wav_bytes
                    temp.write(wav_bytes(pcm,rate));temp.flush()
                    recognized=self.rpc('asr','transcribe',{'pcm_b64':base64.b64encode(pcm).decode(),'rate':rate})
                    text=str(recognized.get('text','')).strip()[:1000]
                    if not text:raise ValueError('Речь не распознана. Повторите или используйте клавиши.')
                    if call.consent:
                        folder=self.store.audio/key;folder.mkdir(exist_ok=True)
                        (folder/(str(len(call.transcript))+'.patient.wav')).write_bytes(Path(temp.name).read_bytes())
                source={'role':'patient','text':text,'source':'real_asr','engine':recognized.get('engine',''),
                        'provider':recognized.get('provider',''),'cuda_verified':recognized.get('cuda_verified',False)}
            elif op=='text':
                text=str(data.get('text','')).strip()
                if not 0<len(text)<=1000:raise ValueError('Введите фразу до 1000 символов')
                source={'role':'patient','text':text,'source':'typed_test'}
            else:
                digit=str(data.get('digit',''))
                if digit not in set('0123456789*#'):raise ValueError('Неизвестная клавиша')
                text='';source={'role':'dtmf','text':digit,'source':'dtmf'}
            source['state']='demo.'+call.state
            source['step_index']=len(call.trace)-1
            call.transcript.append(source)
            prompt=self._advance(call,text,digit)
            call.expires=self.clock()+self.ttl
            result=self._result(key,call,prompt)
            result['recognized']=source if op=='audio' else None
            return result
        finally:call.lock.release()
    @staticmethod
    def _decode(encoded, allow_silence=False):
        if not isinstance(encoded,str) or len(encoded) > (MAX_WAV_BYTES+2)//3*4:
            raise ValueError('Запишите не больше двенадцати секунд речи')
        try:
            raw = base64.b64decode(encoded,validate=True)
            if len(raw)>MAX_WAV_BYTES:raise ValueError('Слишком большая запись')
            with wave.open(io.BytesIO(raw),'rb') as wav:
                channels,width,rate,frames = wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes()
                if channels!=1 or width!=2 or wav.getcomptype()!='NONE' or rate not in {8000,16000,24000,48000}:
                    raise ValueError('Нужен моно WAV PCM16 с частотой 8, 16, 24 или 48 кГц')
                if not rate*(.001 if allow_silence else .15) <= frames <= rate*MAX_SECONDS:
                    raise ValueError('Запишите от одной секунды до двенадцати секунд речи')
                pcm = wav.readframes(frames)
                if len(pcm)!=frames*2:raise ValueError('Запись обрезана')
            values=array.array('h');values.frombytes(pcm)
            import sys
            if sys.byteorder!='little':values.byteswap()
            if not allow_silence and math.sqrt(sum(v*v for v in values)/len(values))<60:
                raise ValueError('Не слышно речи. Проверьте микрофон или используйте клавиши.')
            return pcm,rate
        except (wave.Error,EOFError,TypeError,ValueError) as exc:
            if isinstance(exc,ValueError) and str(exc).startswith(('Нужен','Запишите','Запись','Не слышно','Слишком')):raise
            raise ValueError('Не удалось прочитать WAV. Запишите фразу ещё раз.') from None

    @staticmethod
    def _move(call,state):
        call.state=state;call.trace.append(state);call.attempts=0
        if state=='menu':
            call.selection=None;call.offers=[];call.slot=None

    def enabled(self,call,module):
        return call.modules[module]['enabled'] and self.settings.read()[module]['enabled']

    def _choose(self,call,item):
        call.selection=item
        self._move(call,'referral' if item['referral_required'] else 'confirm')
        return self._prompt(call)

    def _prompt(self,call):
        if call.state=='confirm':return 'Выбрано: '+call.selection['label']+'. Продолжить — один. Назад — ноль. Регистратор — два.'
        if call.analysis and call.state in {'slots','booking_confirm'}:
            return 'В разговоре обсуждается выбор времени.' if call.state=='slots' else 'В разговоре требуется подтверждение выбранного времени.'
        if call.state=='slots':
            return 'Доступные тестовые талоны. '+'. '.join(f'{i+1}: {s["starts_at"][:16].replace("T"," в ")}' for i,s in enumerate(call.offers))+'. Назовите номер варианта. Для регистратора нажмите звёздочку.'
        if call.state=='booking_confirm':return 'Тестовая запись: '+call.selection['label']+', '+call.slot['starts_at'][:16].replace('T',' в ')+'. Подтвердить — один. Выбрать другое время — ноль.'
        return PROMPTS[call.state]

    def _handoff(self,call,reason):
        call.flags.append(reason)
        self._move(call,'operator')
        return PROMPTS['operator']

    def _advance(self,call,text,digit):
        low=re.sub(r'[^а-яё0-9\s]',' ',text.lower()).replace('ё','е').strip()
        low=re.sub(r'\s+',' ',low)
        # Global interruptions precede both number parsing and slot collection.
        if re.search(r'задыха|не могу дышать|потер[а-я]+ сознани|скорая помощь|очень сильн[а-я]* бол',low):
            call.flags.append('emergency');self._move(call,'emergency');return PROMPTS['emergency']
        if re.search(r'\b(?:нах[уе][йр]|на\s+х[уе][йр]|хуй|хер|бля[дт][ьб]?|ебан\w*|пош[ео]л\w*|идиот\w*|заткнись)\b',low):return self._handoff(call,'abusive_language')
        if digit=='*' or re.search(r'оператор|регистратор|жив[а-я]+\s+человек',low):return self._handoff(call,'requested_operator')
        if re.search(r'операци|сустав|диагноз|лечени|какие таблетки|жалоб',low):return self._handoff(call,'nonstandard_medical_request')
        if re.fullmatch(r'(?:здравствуйте|здравствуй|добрый (?:день|вечер)|доброе утро|алло|ало|привет)(?: пожалуйста)?',low):return 'Я вас слышу. '+self._prompt(call)
        if re.fullmatch(r'(?:что|че|чего|а|повтори\w*|не понял\w*|не слыш\w*|и че ты от меня хочешь|что ты от меня хочешь)',low):return 'Повторю вопрос. '+self._prompt(call)
        if re.search(r'пицц|пепперони|доставк[ау]|не хочу.*запис',low):
            call.attempts+=1
            if call.attempts>=2:return self._handoff(call,'out_of_scope')
            return 'Вы позвонили в регистратуру поликлиники. Я помогаю с записью к врачу. '+self._prompt(call)
        if digit=='#':self._move(call,'menu');return PROMPTS['menu']
        if self.enabled(call,'scenario_extensions'):
            for version in sorted(call.extensions,key=lambda v:v['scope']=='global'):
                for event in version['events']:
                    if event['state']==call.state and event['phrase'].casefold().strip()==text.casefold().strip():
                        return event['response']+' '+self._prompt(call)
        if digit is None:
            digit={'один':'1','одна':'1','первый':'1','первое':'1','два':'2','две':'2','второй':'2','три':'3','третий':'3','ноль':'0'}.get(low)
            if low in set('0123456789'):digit=low
        if call.state=='specialist' and digit=='0':return self._handoff(call,'requested_operator')
        if call.state=='identity_name':
            name=re.sub(r'^(?:меня зовут|это|фио)\s+','',text.strip(),flags=re.I).strip(' .!')
            if re.fullmatch(r"[А-Яа-яЁё'-]+(?:\s+[А-Яа-яЁё'-]+){2}",name):
                call.patient_name=' '.join(name.split());self._move(call,'identity_birth');return self._prompt(call)
        elif call.state=='identity_birth':
            from .identity import normalize_birth_date
            try:birth=normalize_birth_date(text,today=date.today()).isoformat()
            except (ValueError,TypeError):birth=None
            if birth:
                if call.analysis:
                    call.flags.append('identity_and_referral_not_verified')
                    self._move(call,'slots');return self._prompt(call)
                call.patient=self.store.lookup_patient(call.key,call.patient_name,birth)
                if not call.patient:return self._handoff(call,'patient_not_found')
                if call.selection['referral_required'] and not self.store.has_referral(call.key,call.patient['id'],call.selection['id']):return self._handoff(call,'referral_not_verified')
                if not self.enabled(call,'crm'):return self._handoff(call,'crm_disabled')
                preference=self.store.saved_preference(call.patient['id']) if self.enabled(call,'preferences') else None
                call.offers=self.store.slots(call.key,call.selection['id'],preference)
                if not call.offers:return self._handoff(call,'no_slots')
                self._move(call,'slots');return self._prompt(call)
        elif call.state=='slots':
            if call.analysis and (digit in {'1','2','3'} or parse_yes_no(text) is True or re.search(r'\b\d{1,2}[:.]\d{2}\b|утр|вечер|завтра',low)):
                self._move(call,'booking_confirm');return self._prompt(call)
            if digit and digit.isdigit() and 1<=int(digit)<=len(call.offers):
                call.slot=call.offers[int(digit)-1];self._move(call,'booking_confirm');return self._prompt(call)
            if not call.analysis and self.enabled(call,'preferences') and call.patient and re.search(r'предпочитаю|мне удобно',low):
                preference=next((value for word,value in [('утр','morning'),('после обеда','afternoon'),('днем','afternoon'),('вечер','evening')] if word in low),None)
                if preference:
                    self.store.preference(call.patient['id'],preference)
                    call.offers=self.store.slots(call.key,call.selection['id'],preference)
                    return 'Предпочтение сохранено. '+(self._prompt(call) if call.offers else self._handoff(call,'no_slots_for_preference'))
        elif call.state=='booking_confirm':
            yes_no=parse_yes_no(text) if text else None
            if digit=='0' or yes_no is False:self._move(call,'slots');return self._prompt(call)
            if digit=='1' or yes_no is True:
                if call.analysis:
                    call.flags.append('booking_status_not_verified')
                    self._move(call,'done');return 'В разговоре прозвучало подтверждение. Результат в МИС не проверен.'
                if not self.enabled(call,'crm'):return self._handoff(call,'crm_disabled')
                try:call.booking=self.store.book(call.key,call.patient['id'],call.slot['id'],call.selection['id'],call.selection['referral_required'])
                except ValueError as exc:
                    call.flags.append('booking_rejected');self._move(call,'slots');call.offers=self.store.slots(call.key,call.selection['id'])
                    return str(exc)+' '+(self._prompt(call) if call.offers else self._handoff(call,'no_slots'))
                self._move(call,'done');return PROMPTS['done']
        elif call.state in {'referral','confirm','therapist_first'}:
            yes_no=parse_yes_no(text) if text else None
            if call.state=='referral' and re.search('направлен',low):
                yes_no=False if re.search(r'\b(?:нет|не|без)\b',low) else (True if re.search(r'\b(?:есть|имеется|получил[аи]?|дали|выдали)\b|на руках',low) else yes_no)
            if digit=='2':return self._handoff(call,'requested_operator')
            if digit=='1' or (digit is None and yes_no is True):
                if call.state=='confirm':self._move(call,'identity_name');return self._prompt(call)
                if call.state=='therapist_first':
                    child=bool(call.selection and call.selection['label'].startswith('Детский '))
                    return self._choose(call,{'id':'pediatrician' if child else 'therapist','label':'Участковый педиатр' if child else 'Участковый терапевт','referral_required':False})
                self._move(call,'confirm');return self._prompt(call)
            if digit=='0' or (digit is None and yes_no is False):
                self._move(call,'therapist_first' if call.state=='referral' else 'menu');return self._prompt(call)
        else:
            if re.search(r'анализ|исследован|узи|экг|рентген',low) or (call.state=='menu' and digit=='3'):
                return self._choose(call,{'id':'diagnostic','label':'Исследование или лабораторные анализы','referral_required':True})
            if re.search('педиатр',low):return self._choose(call,{'id':'pediatrician','label':'Участковый педиатр','referral_required':False})
            if re.search('терапевт|участков',low) or (call.state=='menu' and digit=='1'):
                child=bool(re.search('ребен|детск|доч|сын',low))
                return self._choose(call,{'id':'pediatrician' if child else 'therapist','label':'Участковый педиатр' if child else 'Участковый терапевт','referral_required':False})
            selected=next((s for s in SPECIALISTS if re.search(s[2],low)),None)
            if selected:
                child=bool(re.search('ребен|детск|доч|сын',low))
                label=('Детский '+selected[1].lower()) if child else selected[1]
                return self._choose(call,{'id':selected[0],'label':label,'referral_required':selected[3]})
            if (call.state=='menu' and digit=='2') or re.search('специалист|врач',low):
                self._move(call,'specialist');return PROMPTS['specialist']
        call.attempts+=1
        if call.attempts>=3:return self._handoff(call,'unrecognized_repeatedly')
        return 'Не удалось понять ответ. '+self._prompt(call)

    def document(self,call):
        titles={**TITLES,**({'done':'Подтверждение со слов участников'} if call.analysis else {})}
        return {'call_id':call.key,'sandbox':True,'executed':False,'sandbox_booking':call.booking,'state':'demo.'+call.state,
            'title':titles[call.state],'selection':call.selection,'prompt':call.transcript[-1]['text'] if call.transcript else '',
            'transcript':list(call.transcript),'trace':['demo.'+s for s in call.trace],
            'steps':[{'id':'demo.'+s,'title':titles[s]} for s in call.trace],
            'graph':{'version':POLICY_VERSION,'titles':titles,'branches':BRANCHES},'flags':list(call.flags),
            'consent_audio':call.consent,'finished':call.state in TERMINAL,'catalog_source':CATALOG_SOURCE,
            'offers':list(call.offers),'patient':call.patient,'analysis':call.analysis,'modules':call.modules,
            'extensions':call.extensions,
            'audio_files':[p.name for p in sorted((self.store.audio/call.key).glob('*.wav'))] if call.consent else [],
            'proposed_action':'Создана запись только в тестовой SQLite' if call.booking else 'Запись ещё не создана'}

    def _persist(self,call):
        document=self.document(call)
        self.store.save_call(call.key,call.owner,document)
        if self.enabled(call,'client_tags') and call.patient and call.flags:
            self.store.tag(call.patient['id'],call.flags)

    def _result(self,key,call,prompt):
        call.transcript.append({'role':'assistant','text':prompt,'state':'demo.'+call.state,'step_index':len(call.trace)-1,'source':'scenario'})
        # Save the actual dialogue even if synthesis fails.
        self._persist(call)
        result=self.document(call)
        if self.enabled(call,'tts'):
            try:
                voice=self.rpc('tts','preview',{'text':prompt,'rate':1.3})
                if voice.get('sample_rate')!=48000 or not voice.get('pcm_b64'):raise ValueError('Модуль не вернул речь в качестве сорок восемь килогерц')
                result['audio']=voice
                if call.consent:
                    from .provider_adapter import wav_bytes
                    try:
                        pcm=base64.b64decode(voice['pcm_b64'],validate=True)
                        folder=self.store.audio/key;folder.mkdir(exist_ok=True)
                        (folder/(str(len(call.transcript))+'.assistant.wav')).write_bytes(wav_bytes(pcm,48000))
                    except ValueError: pass
                    self._persist(call)
                    result['audio_files']=self.document(call)['audio_files']
            except ValueError as exc:result['audio_error']=str(exc)
        return result


def speech_phrases():
    demo=DemoCalls();phrases=set(PROMPTS.values())
    for label in ['Участковый терапевт','Участковый педиатр','Исследование или лабораторные анализы']+[v[1] for v in SPECIALISTS]:
        phrases.add(demo._prompt(Call('',0,state='confirm',selection={'label':label})))
    return sorted(phrases)

"""DOCX acceptance against the shipping E5, compiled Rust FSM and HTTP MIS adapter.

IDs are stable source scenario/example/module codes. Missing integration is a failure.
No external LLM, live credentials or actual patient records are used.
"""
from contextlib import contextmanager
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import threading

import pytest

from app.types import MisError, MisTimeout
from calls.crm.registry import MockRegistry, seed_demo
from clinic.crm import ClinicCrm, StrictStand
from clinic.modules import Nlu
from clinic.scenario_catalog import catalog

ROOT = Path(__file__).resolve().parents[2]
CATALOG = catalog()
BINARY = Path(os.environ.get('CONTRACT_REPLAY_BIN', ROOT/'target/debug/contract-replay'))
LABELS = {'С-01':'book','С-02':'book','С-03':'reschedule','С-04':'cancel',
          'С-05':'my_booking','С-06':'home_visit','С-07':'info_schedule','С-08':'results',
          'С-09':'paid','С-11':'waitlist','С-12':'for_other','С-13':'attachment','С-15':'complaint'}
# These annotations are based on the source, never on observed model output.
D_LABELS = {1:['book','emergency'], 2:['medical'], 3:[['medical','results']], 4:['my_booking'],
            5:['operator'], 6:['book'], 7:['cancel'], 8:['book'], 14:['self_identity'],
            15:['record_opt_out'],16:['complaint']}
D_SCENARIOS = {1:'C-14',2:'ALL',3:'C-08',4:'C-05',5:'ALL',6:'C-01',7:'C-04',
               8:'C-02',9:'C-06',10:'C-10',11:'C-10',12:'ALL',13:'ALL',
               14:'ALL',15:'ALL',16:'C-15'}


def ascii_id(identifier):
    return identifier.replace('С-', 'C-')


def trigger_cases():
    result=[]
    for scenario in CATALOG['scenarios']:
        number=0
        for item in scenario['passport'].get('Триггеры', []):
            for literal in re.findall('«([^»]+)»', item['text']):
                number+=1
                # An explicit template instantiation, preserving the literal in failure messages.
                templates={'Запишите к…':'Запишите к терапевту','хочу попасть к…':'хочу попасть к терапевту','Когда принимает…?':'Когда принимает терапевт?','Сколько стоит…?':'Сколько стоит приём?'}
                text=templates.get(literal,literal)
                assert '…' not in text, literal
                result.append({'id':f"{ascii_id(scenario['id'])}.{number:02d}",
                               'scenario':scenario['id'],'text':text,'literal':literal,
                               'paragraph':item['paragraph'],'intent':LABELS[scenario['id']]})
    return result


TRIGGERS = trigger_cases()
DIALOGUES = CATALOG['control_dialogues']


@pytest.fixture(scope='session')
def nlu():
    return Nlu('e5')


def interpretation(nlu,text,**kwargs):
    status, result=nlu.handle('interpret',{'text':text,'today':'2026-10-15',**kwargs})
    assert status=='ok', result
    assert result['semantic']['engine']=='multilingual-e5-small-onnx-int8'
    return result


def probe(events,flow='inbound',variables=None):
    process=subprocess.run([str(BINARY)],input=json.dumps({'flow':flow,'vars':variables or {},'events':events}),
                           text=True,capture_output=True,timeout=20)
    assert process.returncode==0,process.stderr
    return json.loads(process.stdout)['frames']


def speech(nlu,text,**kwargs):
    return {'kind':'speech','value':text,'payload':interpretation(nlu,text,**kwargs)}


def words(frames):
    return ' '.join(a['text'] for f in frames for a in f['actions'] if a['kind']=='say').lower()


def module_calls(frames):
    return [a['args'] for f in frames for a in f['actions'] if a['kind']=='call']


class Conversation:
    """Replay the actual FSM and execute its CRM calls against a fresh real SQLite."""
    def __init__(self,nlu,crm):
        self.nlu,self.crm=nlu,crm
        self.events=[];self.calls=[]
        self.frames=probe([])

    def send(self,text):
        self.events.append(speech(self.nlu,text,identity_field=self.frames[-1].get('input_field')))
        self.frames=probe(self.events)
        for _ in range(32):
            calls=[a for a in self.frames[-1]['actions'] if a['kind']=='call']
            if not calls:return self.frames[-1]
            assert len(calls)==1,calls
            call=calls[0]['args'];assert call['module']=='crm',call
            args={**call['args'],'call_id':'docx-module-loop'}
            status,payload=self.crm.handle(call['op'],args)
            self.calls.append({'op':call['op'],'args':args,'status':status,'payload':payload})
            self.events.append({'kind':'module','value':status,'payload':payload,'correlate':True})
            self.frames=probe(self.events)
        pytest.fail('FSM_CRM_LOOP_LIMIT '+repr(self.calls))


def test_source_all_passports_and_control_examples_have_stable_ids():
    assert CATALOG['source_sha256']=='0d39c2570371e499f177d91241a59009de54dfb431aea23045d21a0eb95364fc'
    assert [s['id'] for s in CATALOG['scenarios']]==[f'С-{n:02d}' for n in range(1,16)]
    assert [d['id'] for d in DIALOGUES]==list(range(1,17))
    assert len({c['id'] for c in TRIGGERS})==len(TRIGGERS)
    assert {c['scenario'] for c in TRIGGERS}==set(LABELS)


@pytest.mark.parametrize('scenario',CATALOG['scenarios'],ids=lambda s:ascii_id(s['id'])+'.source')
def test_source_passport_retains_parameters_mis_and_result(scenario):
    assert scenario['passport'].get('Результат'),scenario['id']
    for items in scenario['passport'].values():
        assert all(isinstance(row['paragraph'],int) and row['text'] for row in items)


@pytest.mark.parametrize('case',TRIGGERS,ids=lambda c:c['id']+'.label')
def test_label_every_source_trigger(nlu,case):
    actual=interpretation(nlu,case['text'])
    assert actual['intent'] in (case['intent'] if isinstance(case['intent'],list) else [case['intent']]),{'source':case,'actual':actual}


CONTROL_UTTERANCES=[]
for dialogue in DIALOGUES:
    if dialogue['id'] in D_LABELS:
        quotations=re.findall('«([^»]+)»',dialogue['input'])
        if dialogue['id']==4:
            assert quotations[1:]==['доверенное лицо']
            quotations=quotations[:1]
        expected=D_LABELS[dialogue['id']]
        assert len(quotations)==len(expected),dialogue
        for index,(text,intent) in enumerate(zip(quotations,expected),1):
            CONTROL_UTTERANCES.append({'id':f"{D_SCENARIOS[dialogue['id']]}.D-{dialogue['id']:02d}.{index:02d}",
                                       'text':text,'intent':intent,'dialogue':dialogue})


@pytest.mark.parametrize('case',CONTROL_UTTERANCES,ids=lambda c:c['id']+'.label')
def test_label_control_utterances(nlu,case):
    actual=interpretation(nlu,case['text'])
    assert actual['intent'] in (case['intent'] if isinstance(case['intent'],list) else [case['intent']]),{'source':case,'actual':actual}


@pytest.mark.parametrize('case',TRIGGERS+CONTROL_UTTERANCES,ids=lambda c:c['id']+'.entities.authority')
def test_entities_source_text_never_supplies_write_authority(nlu,case):
    actual=interpretation(nlu,case['text'])
    assert not {'patient_id','booking_id','slot_id','confirmed','referral_verified'} & actual['vars'].keys(),actual


@pytest.mark.parametrize('field,expected', [('specialty','отоларинголог'),('said_daypart','afternoon'),('excluded_weekdays',[4])],
                         ids=['C-01.D-06.entities.specialty','C-01.D-06.entities.daypart','C-01.D-06.entities.exclude_friday'])
def test_entities_source_time_constraints_survive(nlu,field,expected):
    text=re.findall('«([^»]+)»',DIALOGUES[5]['input'])[0]
    actual=interpretation(nlu,text)
    assert actual['vars'].get(field)==expected,actual


@pytest.mark.parametrize('case',TRIGGERS,ids=lambda c:c['id']+'.fsm.entry')
def test_fsm_trigger_enters_implemented_branch(nlu,case):
    frames=probe([speech(nlu,case['text'])])
    last=frames[-1]
    assert last['vars'].get('reason') not in {'scenario_integration_not_enabled','speech_unclassified'},frames
    assert last['state']!=frames[0]['state'] or module_calls(frames),frames
    assert last['vars'].get('last_intent')==case['intent'],frames


@pytest.mark.parametrize('dialogue',DIALOGUES,ids=lambda d:f"{D_SCENARIOS[d['id']]}.D-{d['id']:02d}.fsm")
def test_fsm_control_dialogues(nlu,dialogue,tmp_path,monkeypatch):
    number=dialogue['id']
    quotes=re.findall('«([^»]+)»',dialogue['input'])
    if number==1:
        frames=probe([speech(nlu,q) for q in quotes])
        assert frames[-1]['status']=='transfer' and frames[-1]['vars'].get('reason')=='emergency',frames
        assert 'сто три' in words(frames) and 'сто двенадцать' in words(frames),frames
    elif number==2:
        frames=probe([speech(nlu,quotes[0])])
        assert frames[-1]['state']=='inbound.medical_boundary',frames
        assert 'не могу' in words(frames) and 'запис' in words(frames),frames
    elif number==3:
        frames=probe([speech(nlu,quotes[0])])
        # Generic drug advice is not the promised results-status/collection branch.
        assert 'результат' in words(frames) and 'готов' in words(frames),frames
        assert not any(c.get('op')=='book' for c in module_calls(frames)),frames
    elif number==4:
        frames=probe([speech(nlu,quotes[0])],variables={'patient_name':'Смирнова Ольга','booking_id':1})
        assert not any(c.get('op') in {'my_booking','select_booking'} for c in module_calls(frames)),frames
        assert 'самому пациенту' in words(frames) and ('сообщение' in words(frames) or 'эс эм эс' in words(frames)),frames
    elif number==5:
        frames=probe([speech(nlu,quotes[0])])
        assert frames[-1]['status']=='transfer' and frames[-1]['vars'].get('reason')=='requested',frames
    elif number==6:
        frames=probe([speech(nlu,quotes[0])])
        assert frames[-1]['vars'].get('said_daypart')=='afternoon',frames
        assert frames[-1]['vars'].get('excluded_weekdays')==[4],frames
    elif number in {7,8}:
        monkeypatch.setenv('CRM_NOW','2026-10-15T11:00:00+04:00')
        monkeypatch.setenv('MODULE_SETTINGS_FILE',str(tmp_path/'modules.json'))
        registry=MockRegistry(str(tmp_path/'dialogue.sqlite'),datetime(2026,10,15,11))
        seed_demo(registry,date(2026,10,16));crm=ClinicCrm(registry)
        if number==7:
            registry.book(3,1);registry.book(3,2)
        dialogue_run=Conversation(nlu,crm)
        dialogue_run.send(quotes[0])
        person=registry.patient_by_id(3)
        dialogue_run.send(person.full_name)
        final=dialogue_run.send(person.birth_date.strftime('%d.%m.%Y'))
        if number==7:
            assert final['state']=='inbound.existing_choose',{'frames':dialogue_run.frames,'calls':dialogue_run.calls}
            assert not any(c['op']=='cancel' for c in dialogue_run.calls)
            final=dialogue_run.send('Первую')
            assert final['state']=='inbound.cancel_offer',{'frames':dialogue_run.frames,'calls':dialogue_run.calls}
            assert 'перенести' in words(dialogue_run.frames)
        else:
            assert final['state']=='inbound.book_no_referral',{'frames':dialogue_run.frames,'calls':dialogue_run.calls}
            assert any(c['op']=='check_referral' for c in dialogue_run.calls),dialogue_run.calls
            assert 'терапевту' in words(dialogue_run.frames)
            assert not registry.execute('SELECT * FROM bookings')
    elif number==9:
        # Situation has no literal utterance. Enter its real source intent directly.
        frames=probe([{'kind':'speech','value':'Вызвать врача на дом','payload':{'intent':'home_visit','source_scenario':'С-06','vars':{'said_time':'15:10','said_date':'2026-10-20'}}}])
        assert frames[-1]['vars'].get('reason')!='scenario_integration_not_enabled',frames
        assert 'завтра' in words(frames) and 'неотлож' in words(frames),frames
    elif number in {10,11}:
        text='Вы ошиблись, здесь другой человек' if number==10 else 'Это автоответчик. Оставьте сообщение после сигнала'
        frames=probe([speech(nlu,text)],flow='reminder',variables={'patient_name':'Секретный Пациент','booking_id':123})
        assert 'секретный' not in words(frames) and '123' not in words(frames),frames
        assert frames[-1]['finished'] and frames[-1]['status'] in {'no_verified_contact','voicemail'},frames
        assert not any(c.get('op') in {'confirm','cancel','reschedule'} for c in module_calls(frames)),frames
    elif number==12:
        frames=probe([{'kind':'speech','value':'неразборчиво','payload':{'intent':'unknown'}}]*3)
        assert 'повтор' in words(frames) or 'не расслыш' in words(frames),frames
        assert frames[-1]['status']=='transfer',frames
    elif number==13:
        # Establish a real pending CRM operation before injecting its timeout.
        events=[speech(nlu,'Запишите к терапевту'),speech(nlu,'Петрова Мария Сергеевна',identity_field='full_name')]
        frames=probe(events)
        if frames[-1]['pending'] is None:
            events.append(speech(nlu,'23.11.1992',identity_field='birth_date'))
            frames=probe(events)
        assert frames[-1]['pending'] is not None,{'error':'SOURCE_MIS_REQUEST_NOT_REACHED','frames':frames}
        events.append({'kind':'module','value':'error','correlate':True,'payload':{'error':'ТАЙМАУТ','elapsed_seconds':6}})
        frames=probe(events)
        assert frames[-1]['status']=='transfer' or frames[-1]['state'].startswith('callback.'),frames
        assert frames[-1]['vars'].get('specialty')=='терапевт',frames
        assert not any(c.get('op') in {'book','cancel','reschedule'} for c in module_calls(frames)),frames
    elif number==14:
        frames=probe([speech(nlu,quotes[0])])
        assert 'виртуальный' in words(frames) and 'сотрудник' in words(frames),frames
        assert not frames[-1]['finished'],frames
    elif number==15:
        frames=probe([speech(nlu,quotes[0])])
        assert frames[-1]['vars'].get('recording_consent') is False,frames
        assert 'отключ' in words(frames) and not frames[-1]['finished'],frames
    elif number==16:
        frames=probe([speech(nlu,quotes[0])])
        assert 'старшему администратору' in words(frames),frames
        assert frames[-1]['status']=='transfer' and frames[-1]['vars'].get('reason')=='complaint',frames
    else:
        pytest.fail('UNIMPLEMENTED_SOURCE_DIALOGUE '+str(number))


_UNSET=object()


@contextmanager
def mis_server(status=200,payload=_UNSET):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({'method':'GET','path':self.path,'authorization':self.headers.get('Authorization')=='Bearer contract-only'})
            raw=json.dumps([] if payload is _UNSET else payload).encode()
            self.send_response(status);self.send_header('Content-Type','application/json')
            if status==429:self.send_header('Retry-After','7')
            self.end_headers();self.wfile.write(raw)
        def log_message(self,*_):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/api/v1',requests
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


MIS_SCENARIOS=[ascii_id(s['id']) for s in CATALOG['scenarios'] if s['passport'].get('Операции МИС')]
HTTP_ERRORS=[(400,'syntax'),(401,'unauthorized'),(403,'forbidden'),(404,'not_found'),(409,'conflict'),(422,'constraint'),(429,'rate_limit'),(503,'unavailable')]
MIS_CASES=[pytest.param(c,status,code,id=f'{c}.MIS.http{status}.{code}') for c in MIS_SCENARIOS for status,code in HTTP_ERRORS]


@pytest.mark.parametrize('scenario,status,code',MIS_CASES)
def test_mis_http_errors_are_not_success_or_fallback(monkeypatch,scenario,status,code):
    monkeypatch.setenv('ALLOW_LOCAL_STAND','1')
    with mis_server(status,{'code':code}) as (url,requests):
        stand=StrictStand(url,'contract-only')
        with pytest.raises(MisError) as raised:stand.request('GET','/visits')
        assert raised.value.status==status,vars(raised.value)
        if status==429:assert raised.value.retry_after==7
        assert len(requests)==1 and requests[0]['authorization']


@pytest.mark.parametrize('payload',[{'rows':[]},None,'invalid'],ids=['object','null','string'])
def test_mis_wrong_result_schema_is_rejected(monkeypatch,payload):
    monkeypatch.setenv('ALLOW_LOCAL_STAND','1')
    with mis_server(200,payload) as (url,requests):
        with pytest.raises(TypeError):StrictStand(url,'contract-only').pages('/doctors')
        assert len(requests)==1


def test_mis_read_uses_expected_api_and_authentication(monkeypatch):
    monkeypatch.setenv('ALLOW_LOCAL_STAND','1')
    with mis_server(200,[]) as (url,requests):
        assert StrictStand(url,'contract-only').pages('/doctors')==[]
        assert requests==[{'method':'GET','path':'/api/v1/doctors?limit=50&offset=0','authorization':True}]


@pytest.mark.parametrize('dialogue_id',[7,8,13],ids=['C-04.D-07.mis.multiple','C-02.D-08.mis.referral','C-01.D-13.mis.uncertain'])
def test_mis_source_business_invariants(tmp_path,monkeypatch,dialogue_id):
    monkeypatch.setenv('CRM_NOW','2026-10-15T11:00:00+04:00')
    registry=MockRegistry(str(tmp_path/'registry.sqlite'),datetime(2026,10,15,11))
    seed_demo(registry,date(2026,10,16));crm=ClinicCrm(registry)
    if dialogue_id==7:
        one=registry.book(3,1);two=registry.book(3,2)
        status,result=crm.handle('my_booking',{'patient_id':3})
        assert status=='many' and result['booking_id'] is None,result
        assert result['booking_candidate_ids']==[one.id,two.id],result
    elif dialogue_id==8:
        slot=registry.execute('SELECT s.id FROM slots s JOIN doctors d ON d.id=s.doctor_id WHERE d.needs_referral=1 LIMIT 1')[0]['id']
        status,result=crm.handle('book',{'patient_id':3,'slot_id':slot,'operation_key':'source-no-referral','has_referral':True})
        assert status=='error' and result['error']=='referral_not_verified',result
        assert not registry.execute('SELECT * FROM bookings')
    else:
        class LostReply:
            def __init__(self):self.calls=[]
            def request(self,method,path,body=None,query=None):
                self.calls.append((method,path))
                if path=='/clients':return {'id':999}
                raise MisTimeout('response lost',0,'ТАЙМАУТ')
        crm.stand=LostReply()
        import time
        crm._last_slots_sync=time.monotonic()
        args={'patient_id':3,'slot_id':1,'operation_key':'source-lost-response'}
        first=crm.handle('book',args);second=crm.handle('book',args)
        assert all(r[0]=='error' and r[1]['operation_state']=='uncertain' for r in (first,second)),(first,second)
        assert sum(path=='/visits' for method,path in crm.stand.calls)==1,crm.stand.calls
        assert registry.execute('SELECT status FROM slots WHERE id=1')[0]['status']=='reserved'


@pytest.mark.parametrize('intent,literal',[
    pytest.param('emergency','ой, у меня вообще-то сильно давит в груди',id='C-14.D-01.audio.prerecorded'),
    pytest.param('complaint','Хочу пожаловаться на регистратуру',id='C-15.D-16.audio.prerecorded'),
    pytest.param('record_opt_out','Я против записи разговора',id='ALL.D-15.audio.prerecorded'),
    pytest.param('self_identity','Вы робот?',id='ALL.D-14.audio.prerecorded'),
])
def test_audio_static_source_actions_must_be_in_prerecordable_flows(intent,literal):
    from calls.scenario.dsl import load_flows
    static={(s.text,float(s.args.get('rate',flow.rate))) for flow in load_flows(ROOT/'scenarios').values()
            for state in flow.states.values() for s in state.statements if s.kind=='say' and '{' not in s.text}
    frames=probe([{'kind':'speech','value':literal,'payload':{'intent':intent,'confidence':1.0}}])
    assert frames[-1]['vars'].get('last_intent')==intent,frames
    emitted=[(a['text'],a['rate']) for frame in frames[1:] for a in frame['actions'] if a['kind']=='say' and a['text']]
    assert emitted and all(text in static for text in emitted),{'intent':intent,'unbanked':[t for t in emitted if t not in static]}

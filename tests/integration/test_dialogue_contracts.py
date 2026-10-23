"""Adversarial contracts use actual sentence embeddings and real isolated SQLite."""
import json
from datetime import date,datetime
from pathlib import Path
import pytest
from clinic.specialty_dictionary import SpecialtyDictionary
from clinic.dialogue_plan import compile_plan,plans
from clinic.semantic import SemanticInterpreter
from clinic.demo_call import DemoCalls
from clinic.module_settings import ModuleSettings
from clinic.sandbox_store import SandboxStore
from clinic.crm import ClinicCrm
from calls.crm.registry import MockRegistry,seed_demo


def dictionary_at(tmp_path):
    path=tmp_path/'specialties.json'
    path.write_text(Path('runtime/src/clinic/specialty_names.json').read_text())
    return SpecialtyDictionary(path)


def test_added_hepatologist_inherits_all_gates_and_invalidates_cached_revision(tmp_path,semantic_nlu,monkeypatch):
    dictionary=dictionary_at(tmp_path)
    before=dictionary.read()
    items=before['specialties']+[{'id':'hepatology','label':'Гепатолог','aliases':['гепатолог','гепатологу','врач по печени'],'referral_required':True}]
    saved=dictionary.save(items,before['revision'])
    graph=plans(dictionary)
    assert graph['revision']!=before['revision']
    names=[n['name'] for n in graph['plans']['hepatology']]
    assert names==['identity_name','identity_birth','identity_lookup','specialty','referral_lookup','availability_lookup','offer','confirmation','commit','receipt']
    assert compile_plan(saved['revision'],'hepatology',True) is compile_plan(saved['revision'],'hepatology',True)
    monkeypatch.setattr(semantic_nlu,'semantic',SemanticInterpreter(encoder=semantic_nlu.semantic.encoder,dictionary=dictionary))
    store=SandboxStore(tmp_path/'stand');settings=ModuleSettings(tmp_path/'modules.json')
    modules=settings.read();modules['tts']['enabled']=False;settings.save(modules)
    def rpc(module,op,args):
        assert module=='nlu' # no live MIS, TTS, SMS or remote APIs
        return semantic_nlu.handle(op,args)[1]
    demo=DemoCalls(rpc=rpc,store=store,settings=settings);demo.dictionary=dictionary
    def req(op,**args):return demo.request('/api/demo-call/'+op,True,args,client='test')
    key=req('start')['call_id']
    result=req('text',call_id=key,text='Мне нужен врач по печени')
    assert result['selection']['id']=='hepatology' and result['state']=='demo.referral'
    req('dtmf',call_id=key,digit='1');req('dtmf',call_id=key,digit='1')
    assert demo.calls[key].state=='identity_name'
    req('text',call_id=key,text='Иванов Иван Иванович')
    result=req('text',call_id=key,text='12.04.1985')
    assert result['state']=='demo.identity_readback' and demo.calls[key].patient is None
    result=req('dtmf',call_id=key,digit='1')
    assert result['state']=='demo.operator' and 'referral_not_verified' in result['flags']
    assert not store.tables()['tables']['bookings']


def test_no_referral_specialty_still_cannot_remove_identity_slot_or_consent(tmp_path):
    dictionary=dictionary_at(tmp_path);doc=dictionary.read()
    dictionary.save(doc['specialties']+[{'id':'hepatology','label':'Гепатолог','aliases':['гепатологу'],'referral_required':False}],doc['revision'])
    names=[n['name'] for n in plans(dictionary)['plans']['hepatology']]
    assert 'referral_lookup' not in names
    for required in ['identity_name','identity_birth','identity_lookup','availability_lookup','confirmation','commit','receipt']:assert required in names
    with pytest.raises(ValueError):dictionary.save(dictionary.read()['specialties'],doc['revision'])


@pytest.mark.parametrize('utterance',[
    'Я не хочу отменять запись',
    'Ничего не подтверждаю, я только спрашиваю',
    'Перенесите меня, но я ещё не знаю куда',
    'Мне нужно попасть к врачу по поводу здоровья',
])
def test_free_text_never_carries_write_authority(semantic_nlu,utterance):
    _,payload=semantic_nlu.handle('interpret',{'text':utterance,'patient_id':42,'slot_id':5,'confirmed':True})
    assert not {'patient_id','slot_id','booking_id','has_referral','confirmed'} & payload['vars'].keys()
    assert payload['semantic']['engine']=='multilingual-e5-small-onnx-int8'


def test_negated_specialty_is_not_reintroduced_by_encoder(semantic_nlu):
    _,payload=semantic_nlu.handle('interpret',{'text':'Хочу записаться, но не к хирургу'})
    assert payload['vars'].get('specialty')!='хирург'
    assert payload['vars'].get('specialty_candidate')!='хирург'


def test_crm_rejects_spoken_referral_and_wrong_owner_and_stale_live_snapshot(tmp_path,monkeypatch):
    monkeypatch.setenv('CRM_NOW','2026-10-15T11:00:00+04:00')
    reg=MockRegistry(str(tmp_path/'crm.sqlite'),datetime(2026,10,15,11));seed_demo(reg,date(2026,10,16));crm=ClinicCrm(reg)
    # A real row needing a referral cannot be booked using caller-supplied booleans.
    slot=reg.execute("SELECT s.id FROM slots s JOIN doctors d ON d.id=s.doctor_id WHERE d.needs_referral=1 LIMIT 1")[0]['id']
    result=crm.handle('book',{'operation_key':'no-referral','patient_id':3,'slot_id':slot,'has_referral':True,'referral_verified':True})
    assert result==('error',{'error':'referral_not_verified'})
    _,booking=crm.handle('book',{'operation_key':'valid','patient_id':3,'slot_id':1})
    assert 'booking_id' in booking
    assert crm.handle('cancel',{'operation_key':'wrong-owner','patient_id':1,'booking_id':booking['booking_id']})==('error',{'error':'booking_patient_mismatch'})
    crm.stand=object()
    assert crm.handle('book',{'operation_key':'stale','patient_id':3,'slot_id':2})==('error',{'error':'availability_not_synchronized'})


def test_new_specialty_without_approved_booking_policy_is_not_assumed_to_need_referral(tmp_path):
    dictionary=dictionary_at(tmp_path);doc=dictionary.read()
    items=doc['specialties']+[{'id':'hepatology','label':'Гепатолог','aliases':['гепатологу']}]
    dictionary.save(items,doc['revision'])
    assert dictionary.policy('hepatology')['referral_required'] is None
    assert 'policy_review' in [n['name'] for n in plans(dictionary)['plans']['hepatology']]


def test_catalog_contains_source_passports_instead_of_generated_operation_scenario():
    from clinic.scenario_catalog import catalog
    doc=catalog()
    assert doc['source_sha256']=='0d39c2570371e499f177d91241a59009de54dfb431aea23045d21a0eb95364fc'
    assert [s['id'] for s in doc['scenarios']]==[f'С-{i:02d}' for i in range(1,16)]
    assert len(doc['control_dialogues'])==16
    assert next(s for s in doc['scenarios'] if s['id']=='С-01')['passport']['Параметры'][0]['paragraph']==650
    assert not any('операцию' in s['title'] for s in doc['scenarios'])


def test_multiple_appointments_require_explicit_choice(tmp_path):
    reg=MockRegistry(str(tmp_path/'multiple.sqlite'),datetime(2026,10,15,11));seed_demo(reg,date(2026,10,16));crm=ClinicCrm(reg)
    first=reg.book(3,1);second=reg.book(3,2)
    status,options=crm.module.op_my_booking(3)
    assert status=='many' and options['booking_id'] is None
    assert options['booking_candidate_ids']==[first.id,second.id]
    assert crm.module.op_select_booking(1,options['booking_candidate_ids'],2)==('error',{})
    status,chosen=crm.module.op_select_booking(3,options['booking_candidate_ids'],2)
    assert status=='ok' and chosen['booking_id']==second.id


def test_new_specialty_completes_only_after_all_required_steps(tmp_path,semantic_nlu,monkeypatch):
    dictionary=dictionary_at(tmp_path);doc=dictionary.read()
    dictionary.save(doc['specialties']+[{'id':'hepatology','label':'Гепатолог','aliases':['гепатологу','гепатолог'],'referral_required':True}],doc['revision'])
    monkeypatch.setattr(semantic_nlu,'semantic',SemanticInterpreter(encoder=semantic_nlu.semantic.encoder,dictionary=dictionary))
    store=SandboxStore(tmp_path/'stand');settings=ModuleSettings(tmp_path/'modules.json');values=settings.read();values['tts']['enabled']=False;settings.save(values)
    store.db.execute("INSERT INTO referrals VALUES(1,'hepatology','2099-01-01')");store.db.commit()
    demo=DemoCalls(rpc=lambda module,op,args:semantic_nlu.handle(op,args)[1],store=store,settings=settings);demo.dictionary=dictionary
    def req(op,**args):return demo.request('/api/demo-call/'+op,True,args,client='new-specialty')
    key=req('start')['call_id'];req('text',call_id=key,text='Запишите к гепатологу')
    assert not store.tables()['tables']['bookings']
    req('dtmf',call_id=key,digit='1');req('dtmf',call_id=key,digit='1')
    req('text',call_id=key,text='Иванов Иван Иванович');r=req('text',call_id=key,text='12.04.1985')
    assert r['state']=='demo.identity_readback' and demo.calls[key].patient is None
    r=req('dtmf',call_id=key,digit='1')
    assert r['state']=='demo.slots' and not store.tables()['tables']['bookings']
    r=req('dtmf',call_id=key,digit='1')
    assert r['state']=='demo.booking_confirm' and not store.tables()['tables']['bookings']
    r=req('dtmf',call_id=key,digit='1')
    assert r['state']=='demo.done' and len(store.tables()['tables']['bookings'])==1
    assert r['selection']['id']=='hepatology' and len(r['compiled_plan'])==10

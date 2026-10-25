"""Real HTTP authentication/ACLs. Only CRM content and TTS waveform are fixture dependencies."""
from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import threading
import pytest
from clinic.scenario_editor import Repository,make_server

ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture
def editor(tmp_path,monkeypatch):
    shutil.copytree(ROOT/'scenarios',tmp_path/'scenarios')
    monkeypatch.setenv('EDITOR_PASSWORD_FILE',str(tmp_path/'password.json'))
    calls=[]
    def module_call(module,operation,args):
        calls.append((module,operation,args))
        if operation=='learning.call':return {'call_id':args['call_id'],'registrar':args['registrar'],'trace':['inbound.welcome'],'transcript':[{'text':'Тестовая реплика','state':'inbound.welcome','role':'patient'}]}
        if operation=='learning.calls':return {'calls':[],'registrar':args['registrar']}
        if operation=='learning.cases':return {'cases':[],'badges':{},'labelled_examples':0}
        if operation=='learning.propose':return {'registrar':args['registrar'],'status':'queued'}
        if operation=='preview':return {'pcm_b64':'fixture','sample_rate':48000}
        raise AssertionError(operation)
    monkeypatch.setattr('clinic.workshop.module_call',module_call)
    monkeypatch.setattr('clinic.admin_rpc.module_call',module_call)
    repo=Repository(tmp_path,tmp_path/'data');server=make_server(repo,ROOT/'scenario-editor/dist',port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def request(path,body=None,cookie=None):
        connection=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        headers={'Content-Type':'application/json'}
        if cookie:headers['Cookie']=cookie
        connection.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
        response=connection.getresponse();raw=response.read();result=(response.status,json.loads(raw),response.getheader('Set-Cookie'));connection.close();return result
    yield request,calls
    server.shutdown();server.server_close();thread.join(timeout=5)


def test_observer_reads_steps_and_call_detail_but_cannot_mutate(editor):
    request,calls=editor
    assert request('/api/me')[1]['role']=='observer'
    assert request('/api/workshop/step')[0]==200
    assert request('/api/workshop/call?id=test-call&registrar=101')[1]['transcript'][0]['text']=='Тестовая реплика'
    for route in ['/api/workshop/propose','/api/workshop/preview','/api/workshop/night','/api/save','/api/accounts','/api/audio/replace']:
        assert request(route,{})[0] in {401,403}
    assert request('/api/project')[0]==401
    assert not any(op in {'learning.propose','preview'} for _,op,_ in calls)


def test_default_reference_links_and_their_contract_are_reachable(editor):
    request,_=editor
    profiles=request('/api/workshop/connections')[1]
    assert profiles['mis']['reference'] and profiles['database']['reference']
    status,mis,_=request(profiles['mis']['base_url'])
    assert status==200 and mis['service']=='reference_mis'
    status,contract,_=request(mis['schema'])
    assert status==200 and contract['id']=='mis:contract'
    status,database,_=request(profiles['database']['base_url'])
    assert status==200 and 'patients' in database['tables']


def test_user_user_has_bound_registrar_identity_and_actual_preview_route(editor):
    request,calls=editor
    status,_,cookie=request('/api/login',{'username':'user','password':'user'})
    assert status==200
    assert request('/api/me',cookie=cookie)[1]['role']=='registrar'
    assert request('/api/me',cookie=cookie)[1]['registrar']=='100'
    request('/api/workshop/calls?registrar=other-number',cookie=cookie)
    assert calls[-1][2]['registrar']=='100'
    status,data,_=request('/api/workshop/propose',{'registrar':'other-number'},cookie)
    assert status==200 and data['registrar']=='100'
    for route in ['/api/project','/api/accounts','/api/audio']:
        assert request(route,cookie=cookie)[0]==403
    status,data,_=request('/api/workshop/preview',{'text':'Мы отправим СМС.'},cookie)
    assert status==200 and data['sample_rate']==48000
    assert calls[-1][1]=='preview'
    request('/api/logout',{},cookie)
    assert request('/api/me',cookie=cookie)[1]['role']=='observer'


def test_admin_keeps_password_protection_and_can_add_registrar(editor):
    request,_=editor
    assert request('/api/login',{'username':'admin','password':'user'})[0]==400
    status,_,cookie=request('/api/login',{'username':'admin','password':'admin'})
    assert status==200 and 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
    assert request('/api/project',cookie=cookie)[0]==200
    assert request('/api/audio/preview',{'text':'Тестовая фраза'},cookie)[0]==200
    assert request('/api/accounts',{'username':'staff102','registrar':'102','password':'temporary password 102'},cookie)[0]==200
    assert request('/api/login',{'username':'staff102','password':'temporary password 102'})[0]==200

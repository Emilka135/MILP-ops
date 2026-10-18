"""Anonymous demo API keeps editor mutations protected and enforces same origin."""
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
    modules=[]
    def rpc(module,op,args):
        modules.append((module,op,args))
        assert (module,op)==('tts','preview')
        return {'sample_rate':48000,'pcm_b64':'explicit HTTP fixture, not acoustic acceptance'}
    monkeypatch.setattr('clinic.demo_call.module_call',rpc)
    repo=Repository(tmp_path,tmp_path/'data')
    server=make_server(repo,ROOT/'scenario-editor/dist',port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    revision=repo.read()['revision']
    def request(route,body=None,origin=None):
        connection=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        headers={'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        connection.request('POST' if body is not None else 'GET',route,json.dumps(body) if body is not None else None,headers)
        response=connection.getresponse();result=response.status,json.loads(response.read());connection.close();return result
    yield request,repo,revision,modules
    server.shutdown();server.server_close();thread.join(timeout=5)


def test_anonymous_demo_start_dtmf_does_not_unlock_editor_writes(editor):
    request,repo,revision,modules=editor
    status,started=request('/api/demo-call/start',{})
    assert status==200 and started['sandbox'] and started['audio']['sample_rate']==48000
    key=started['call_id']
    assert request('/api/demo-call/dtmf',{'call_id':key,'digit':'1'})[1]['state']=='demo.confirm'
    assert request('/api/demo-call/dtmf',{'call_id':key,'digit':'1'})[1]['executed'] is False
    for route in ['/api/save','/api/build','/api/accounts','/api/workshop/propose','/api/workshop/night','/api/audio/replace']:
        assert request(route,{})[0] in {401,403}
    assert repo.read()['revision']==revision
    assert all(module=='tts' and op=='preview' for module,op,_ in modules)


def test_cross_origin_and_unknown_operations_are_rejected_before_rpc(editor):
    request,repo,revision,modules=editor
    assert request('/api/demo-call/start',{},origin='https://unrelated.example')[0]==403
    assert not modules
    for op in ['book','cancel','../save','audio/replace']:
        assert request('/api/demo-call/'+op,{'module':'crm','operation':'book'})[0]==400
    assert request('/api/demo-call/start')[0]==400
    assert not modules and repo.read()['revision']==revision
    assert request('/api/demo-call/catalog')[0]==200

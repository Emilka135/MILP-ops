"""Role and origin boundary for module settings, saved transcripts and events."""
from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import threading
import pytest
from clinic.scenario_editor import Repository, make_server

ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture
def http(tmp_path,monkeypatch):
    shutil.copytree(ROOT/'scenarios',tmp_path/'scenarios')
    monkeypatch.setenv('EDITOR_PASSWORD_FILE',str(tmp_path/'password.json'))
    monkeypatch.setattr('clinic.demo_call.module_call',lambda *a:{'sample_rate':48000,'pcm_b64':'AAE='})
    repo=Repository(tmp_path,tmp_path/'data')
    server=make_server(repo,ROOT/'scenario-editor/dist',port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def req(route,body=None,cookie='',origin=''):
        conn=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        headers={'Content-Type':'application/json','Cookie':cookie}
        if origin:headers['Origin']=origin
        conn.request('POST' if body is not None else 'GET',route,json.dumps(body) if body is not None else None,headers)
        response=conn.getresponse();result=response.status,json.loads(response.read()),response.getheader('Set-Cookie','').split(';')[0];conn.close();return result
    yield req
    server.shutdown();server.server_close();thread.join(5);repo.db.close()


def test_admin_can_inspect_but_anonymous_and_registrar_cannot_read_logs(http):
    req=http
    admin=req('/api/login',{'username':'admin','password':'admin'})[2]
    staff=req('/api/login',{'username':'user','password':'user'})[2]
    for route in ['/api/settings/modules','/api/calls','/api/calls/detail?id=unknown','/api/sandbox/tables']:
        assert req(route)[0]==401
        assert req(route,cookie=staff)[0]==403
    assert req('/api/settings/modules',cookie=admin)[0]==200
    key=req('/api/demo-call/start',{})[1]['call_id']
    req('/api/demo-call/text',{'call_id':key,'text':'да иди ты нахер'})
    assert req('/api/calls/detail?id='+key,cookie=admin)[1]['flags']==['abusive_language']
    assert req('/api/calls',cookie=admin)[1]['calls'][0]['id']==key


def test_settings_require_admin_and_same_origin(http):
    admin=http('/api/login',{'password':'admin'})[2]
    values=http('/api/settings/modules',cookie=admin)[1]['modules']
    values['tts']['enabled']=False
    assert http('/api/settings/modules',{'modules':values},cookie=admin,origin='https://foreign.invalid')[0]==403
    assert http('/api/settings/modules',{'modules':values},cookie=admin)[0]==200
    assert not http('/api/settings/modules',cookie=admin)[1]['modules']['tts']['enabled']
    assert 'audio' not in http('/api/demo-call/start',{})[1]


def test_staff_compilation_uses_its_line_and_replay_cannot_write_bookings(http):
    admin=http('/api/login',{'password':'admin'})[2]
    staff=http('/api/login',{'username':'user','password':'user'})[2]
    values=http('/api/settings/modules',cookie=admin)[1]['modules'];values['scenario_extensions']['enabled']=True
    http('/api/settings/modules',{'modules':values},cookie=admin)
    r=http('/api/extensions/add',{'scope':'global','phrase':'где вход','response':'Справа','state':'menu'},cookie=staff)
    assert r[0]==200 and r[1]['scope']=='line:100'
    assert http('/api/extensions/compile',{'scope':'global'},cookie=staff)[1]['scope']=='line:100'
    assert http('/api/extensions/list?scope=global',cookie=admin)[1]['events']==[]
    r=http('/api/calls/import',{'text':'Пациент: К глазнику\nСотрудник: Записала'},cookie=admin)
    assert r[0]==200 and r[1]['analysis'] and r[1]['state']=='demo.confirm'
    assert http('/api/sandbox/tables',cookie=admin)[1]['tables']['bookings']==[]

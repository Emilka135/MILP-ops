"""Real SQL/reference MIS and configurable HTTP boundary; no live writes."""
import json
from pathlib import Path
from types import SimpleNamespace
import io
import pytest
from clinic.sandbox_store import SandboxStore
from clinic.workbench import Connections,Workbench

@pytest.fixture
def workbench(tmp_path):
    store=SandboxStore(tmp_path/'db');app=Workbench(store,Connections(tmp_path/'connections.json'))
    yield app
    store.db.close()

OBSERVER={'role':'observer'}
STAFF={'role':'registrar'}

def test_default_links_resolve_to_bundled_services(workbench):
    assert workbench.public()['mis']['base_url']=='/api/workshop/reference-mis'
    assert workbench.public()['database']['reference'] is True
    sql=workbench.request('/api/workshop/sql',{'sql':'SELECT full_name FROM patients WHERE id=?','params':[1]},OBSERVER)
    assert sql['result']==[{'full_name':'Иванов Иван Иванович'}] and sql['network_requests']==0
    api=workbench.request('/api/workshop/request',{'method':'GET','path':'/doctors'},OBSERVER)
    assert api['result'] and api['http_status']==200 and api['reference']

@pytest.mark.parametrize('sql',['DELETE FROM patients','DROP TABLE patients','ATTACH DATABASE \'/tmp/other.sqlite\' AS other','SELECT * FROM calls','SELECT * FROM queries','SELECT load_extension(\'x\')','SELECT 1; DELETE FROM patients'])
def test_sql_cannot_mutate_without_consent_or_read_private_logs(workbench,sql):
    with pytest.raises(ValueError):workbench.request('/api/workshop/sql',{'sql':sql,'params':[]},STAFF)
    assert workbench.store.db.execute('SELECT count(*) FROM patients').fetchone()[0]>0

def test_reference_write_is_transactional_and_requires_employee_and_confirmation(workbench):
    data={'sql':'INSERT INTO patients(id,full_name,birth_date) VALUES(?,?,?)','params':[990,'Тестовая Запись Отчество','2001-01-01'],'confirm_write':True}
    with pytest.raises(ValueError):workbench.request('/api/workshop/sql',data,OBSERVER)
    assert workbench.request('/api/workshop/sql',data,STAFF)['changed_rows']==1
    with pytest.raises(ValueError):workbench.request('/api/workshop/sql',data,STAFF)
    assert workbench.request('/api/workshop/sql',{'sql':'SELECT id FROM patients WHERE id=990'},STAFF)['result']==[{'id':990}]

def test_recursive_sql_has_a_deadline(workbench):
    with pytest.raises(ValueError,match='interrupted'):
        workbench.request('/api/workshop/sql',{'sql':'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n'},OBSERVER)


def test_manual_sql_accepts_the_real_identity_query_including_json_cohort(workbench):
    workbench.store.identity_candidates('probe',{'last_name':'Иванов'},birth='1985-04-12',cohort=[1])
    actual=workbench.store.queries('probe')[-1]
    result=workbench.request('/api/workshop/sql',{'sql':actual['sql'],'params':actual['params']},STAFF)
    assert result['result']==actual['result'] and result['result'][0]['id']==1

def test_unknown_mis_operation_never_runs(workbench):
    with pytest.raises(ValueError):workbench.request('/api/workshop/request',{'method':'POST','path':'/invented','confirm_write':True},STAFF)

def test_mis_client_creation_requires_consent_and_returns_real_reference_result(workbench):
    data={'method':'POST','path':'/clients','body':{'last_name':'Тестов','first_name':'Пётр','middle_name':'Ильич','birth_date':'12.04.1985'}}
    with pytest.raises(ValueError,match='Подтвердите'):workbench.request('/api/workshop/request',data,STAFF)
    result=workbench.request('/api/workshop/request',{**data,'confirm_write':True},STAFF)
    assert result['result']['id'] and result['reference'] and result['network_requests']==0

def test_changed_connection_is_used_and_secret_is_never_public(workbench,monkeypatch):
    current=workbench.connections.read();values={key:current[key] for key in ('mis','database')}
    values['mis'].update(adapter='http_mis',base_url='https://mis.example/api/v1',key_env='TEST_MIS_KEY')
    workbench.connections.save(values,current['revision']);monkeypatch.setenv('TEST_MIS_KEY','never-publish-this')
    calls=[]
    def response(request,timeout):calls.append((request.full_url,request.headers,timeout));return io.BytesIO(b'[{"id":123}]')
    monkeypatch.setattr('clinic.workbench.open_http',response)
    result=workbench.request('/api/workshop/request',{'method':'GET','path':'/doctors'},STAFF)
    assert calls[0][0]=='https://mis.example/api/v1/doctors' and calls[0][2]==8
    assert result['result']==[{'id':123}] and not result['reference']
    assert 'never-publish-this' not in json.dumps(workbench.public())
    with pytest.raises(ValueError):workbench.request('/api/workshop/request',{'method':'GET','path':'/doctors'},OBSERVER)
    with pytest.raises(ValueError):workbench.connections.save(values,current['revision'])

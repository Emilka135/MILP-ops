"""Shared scenario source stays readable without CRM/models and cannot expose arbitrary files."""
import json
from pathlib import Path
import shutil

import pytest

from clinic.scenario_editor import Repository
from clinic.scenario_knowledge import ScenarioKnowledge
from clinic.workshop import request

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture
def repository(tmp_path):
    shutil.copytree(ROOT/'scenarios',tmp_path/'scenarios')
    return Repository(tmp_path,tmp_path/'data')


def test_shared_pack_contains_real_flows_docs_and_contract(repository):
    knowledge=ScenarioKnowledge(repository.root,repository.read())
    index=knowledge.index()
    assert {f['name'] for f in index['flows']}=={f['name'] for f in repository.read()['flows']}
    assert all(f['state_count']>0 and f['source_document']=='flow:'+f['name'] for f in index['flows'])
    assert len(index['operations'])==12
    assert not any('referral' in op['operation_id'].lower() for op in index['operations'])
    assert index['referrals_source']=='optional_local_clinic_registry'
    doc=knowledge.document('flow:inbound')
    assert doc['content']==(repository.root/'scenarios/inbound.flow').read_text()
    assert 'local' in knowledge.document('skills:ai')['content'] or 'локальн' in knowledge.document('skills:ai')['content']
    assert 'paths' in json.loads(knowledge.document('mis:contract')['content'])


def test_document_and_flow_revisions_change_with_actual_source(repository):
    before=ScenarioKnowledge(repository.root,repository.read()).index()['revision']
    path=repository.root/'scenarios/README.md'
    path.write_text(path.read_text()+'\nНовая проверенная инструкция.\n')
    assert ScenarioKnowledge(repository.root,repository.read()).index()['revision']!=before
    project=repository.read();project['flows'][0]['states'][0]['label']='Проверенное название'
    saved=repository.save(project,project['revision'])
    result=ScenarioKnowledge(repository.root,saved).flow(saved['flows'][0]['name'])
    assert result['revision']==saved['revision'] and result['flow']['states'][0]['label']=='Проверенное название'


@pytest.mark.parametrize('identifier',['../../secrets/openai.key','/etc/passwd','file:///etc/passwd','docs:missing'])
def test_arbitrary_paths_never_become_document_ids(repository,identifier):
    with pytest.raises(ValueError):ScenarioKnowledge(repository.root,repository.read()).document(identifier)


def test_symlink_outside_pack_is_excluded(repository,tmp_path):
    secret=tmp_path/'private.md';secret.write_text('not a scenario document')
    (repository.root/'scenarios/docs/escape.md').symlink_to(secret)
    knowledge=ScenarioKnowledge(repository.root,repository.read())
    assert not any(d['id']=='docs:escape' for d in knowledge.index()['documents'])
    with pytest.raises(ValueError):knowledge.document('docs:escape')


@pytest.mark.parametrize('role',['observer','registrar','admin'])
def test_knowledge_uses_no_crm_or_model(repository,monkeypatch,role):
    monkeypatch.setattr('clinic.workshop.module_call',lambda *_:pytest.fail('knowledge called a runtime module'))
    result=request(repository,'/api/workshop/knowledge',False,{}, {},{'role':role,'registrar':'100'})
    assert result['flows'] and result['documents']


def test_manual_request_is_validated_offline_not_authorized(repository,monkeypatch):
    monkeypatch.setattr('clinic.workshop.module_call',lambda *_:pytest.fail('draft called MIS'))
    result=request(repository,'/api/workshop/mis-draft',True,{'operation_id':'createVisit'}, {},{'role':'registrar','registrar':'100'})
    assert result['execution']=='draft_only' and result['network_requests']==0
    assert result['missing_fields'] and not result['complete'] and not result['authorized']
    with pytest.raises(ValueError):request(repository,'/api/workshop/mis-draft',True,{}, {},{'role':'observer'})


def test_legacy_source_paths_resolve_to_single_canonical_files():
    for old,new in [('docs/openapi.json','scenarios/mis/openapi.json'),('data/reglament.txt','scenarios/reference/reglament.txt'),('data/acceptance/docx_index.json','scenarios/reference/docx_index.json')]:
        assert (ROOT/old).resolve()==(ROOT/new).resolve()

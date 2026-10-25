import io
import json
from types import SimpleNamespace

from clinic.identity_form import DialogueModel


def test_actual_boundary_call_is_distinct_from_disabled_or_failed(monkeypatch):
    for key in ('CI','GITHUB_ACTIONS','ACT','CLINIC_ACCEPTANCE_TEST'):
        monkeypatch.delenv(key,raising=False)
    item={'enabled':True,'provider':'local','model':'another-local-model','base_url':'http://local-model:11434/v1'}
    model=DialogueModel(SimpleNamespace(read=lambda:{'dialogue':item}))
    calls=[]
    def response(request,timeout):
        calls.append((request.full_url,json.loads(request.data),timeout))
        return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({'field':'goal','question':'К какому врачу хотите записаться?'})}}]}).encode())
    monkeypatch.setattr('clinic.identity_form.open_http',response)
    events=[]
    with model.track(events,'demo.menu'):
        assert model.clarification('непонятная реплика','menu','Уточните цель?')=='К какому врачу хотите записаться?'
    assert calls[0][0]=='http://local-model:11434/v1/chat/completions'
    assert calls[0][1]['model']=='another-local-model' and calls[0][2]==8
    assert events[0]['request_sent'] and events[0]['status']=='responded' and events[0]['duration_ms']>=0
    assert 'непонятная' not in json.dumps(events,ensure_ascii=False)
    monkeypatch.setenv('CLINIC_ACCEPTANCE_TEST','1')
    with model.track(events,'demo.menu'):model.clarification('test','menu','Уточните цель?')
    assert len(calls)==1 and events[-1]['status']=='disabled_in_ci' and not events[-1]['request_sent']


def test_unavailable_model_keeps_deterministic_question(monkeypatch):
    for key in ('CI','GITHUB_ACTIONS','ACT','CLINIC_ACCEPTANCE_TEST'):monkeypatch.delenv(key,raising=False)
    model=DialogueModel(SimpleNamespace(read=lambda:{'dialogue':{'enabled':True,'provider':'local','model':'local'}}))
    monkeypatch.setattr('clinic.identity_form.open_http',lambda *a,**kw:(_ for _ in ()).throw(OSError('offline')))
    events=[]
    with model.track(events,'demo.menu'):assert model.clarification('test','menu','К какому врачу хотите записаться?')=='К какому врачу хотите записаться?'
    assert events[0]['status']=='unavailable' and events[0]['request_sent']

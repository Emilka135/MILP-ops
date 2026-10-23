"""Bounded generative fallback; it cannot choose a slot or call MIS."""
from datetime import date
import json
from types import SimpleNamespace

import pytest
from clinic.availability_constraints import matches, parse, validate
from clinic.identity_form import DialogueModel
from clinic.temporal_dialogue import QUESTION, TemporalDialogue

TODAY = date(2026, 10, 23)
TEXT = 'не в пятницу, после обеда и до полуночи'


def response(**updates):
    return dict(needs_clarification=False, time_from='12:00', time_to='24:00',
                time_from_inclusive=True, time_to_inclusive=False, evidence=TEXT, question='', **updates)


class Model:
    def __init__(self, answer):
        self.answer, self.calls = answer, []
    def complete(self, task, schema):
        self.calls.append(task)
        return self.answer


def test_unknown_bounds_invoke_model_preserve_bans_and_require_confirmation():
    model = Model(response())
    service = TemporalDialogue(model)
    original = parse(TEXT, TODAY)
    assert original['needs_clarification']
    status, result = service.resolve(TEXT, original, today=TODAY.isoformat())
    assert status == 'proposal' and len(model.calls) == 1
    proposed = result['availability_proposal']
    assert proposed['excluded_weekdays'] == [4]
    assert proposed['time_from'] == '12:00' and proposed['time_to'] == '24:00'
    assert 'полуночи' in result['availability_question']
    assert 'availability_constraints' not in result and 'slot_id' not in result
    status, confirmed = service.confirm(proposed)
    assert status == 'ok' and not confirmed['availability_constraints']['needs_clarification']
    assert matches(confirmed['availability_constraints'], '2026-10-24T23:59')
    assert not matches(confirmed['availability_constraints'], '2026-10-23T23:59')
    assert not matches(confirmed['availability_constraints'], '2026-10-24T11:59')


def test_understood_reply_needs_no_generative_request():
    model = Model(None)
    status, result = TemporalDialogue(model).resolve('после 14:00 и до 18:00', today=TODAY.isoformat())
    assert status == 'proposal' and not model.calls
    assert result['availability_resolution_source'] == 'parser'
    assert result['availability_proposal']['time_to'] == '18:00'


@pytest.mark.parametrize('change', [
    {'time_from': '25:00'}, {'time_from': '24:00'}, {'time_to': '24:01'},
    {'time_from': '18:00', 'time_to': '12:00'}, {'time_from_inclusive': 'yes'},
    {'time_to': "16:00';DROP TABLE slots;--"}, {'evidence': 'то, чего пациент не говорил'},
    {'slot_id': 12}, {'patient_id': 1}, {'needs_clarification': 'false'},
])
def test_malformed_or_unsupported_model_output_only_asks_again(change):
    answer = response(); answer.update(change)
    status, result = TemporalDialogue(Model(answer)).resolve(TEXT, parse(TEXT, TODAY), today=TODAY.isoformat())
    assert status == 'clarify' and result['availability_proposal'] is None
    assert 'availability_constraints' not in result


@pytest.mark.parametrize('question', ['Вы уже записаны?', 'Пришлите паспорт?', '{patient_id}?', '<script>?</script>'])
def test_model_cannot_inject_a_false_success_or_identity_request(question):
    answer = response();answer.update(needs_clarification=True, question=question)
    status, result = TemporalDialogue(Model(answer)).resolve(TEXT, today=TODAY.isoformat())
    assert status == 'clarify' and result['availability_question'] == QUESTION


def test_model_forms_a_specific_question_when_the_patient_boundary_is_unknown():
    answer = response();answer.update(needs_clarification=True, question='Во сколько у вас заканчивается рабочий день?')
    status, result = TemporalDialogue(Model(answer)).resolve('после работы до полуночи', today=TODAY.isoformat())
    assert status == 'clarify' and result['availability_question'] == answer['question']
    assert not result['availability_proposal']


@pytest.mark.parametrize('flag', ['CI', 'ACT', 'GITHUB_ACTIONS', 'CLINIC_ACCEPTANCE_TEST'])
def test_actions_never_calls_a_generative_time_api(monkeypatch, flag):
    monkeypatch.setenv(flag, 'true')
    monkeypatch.setattr('clinic.identity_form.open_http', lambda *_a, **_kw: pytest.fail('generative network request in CI'))
    settings = SimpleNamespace(read=lambda: {'dialogue': {'enabled': True, 'provider': 'local', 'model': 'test'}})
    status, result = TemporalDialogue(DialogueModel(settings)).resolve(TEXT, today=TODAY.isoformat())
    assert status == 'clarify' and not result['availability_proposal']


@pytest.mark.parametrize('provider', ['local', 'compatible'])
def test_both_model_api_adapters_roundtrip_json_and_bound_the_request(monkeypatch, provider):
    # Transport contract; this is not a claim of calling an external provider.
    for flag in ('CI', 'ACT', 'GITHUB_ACTIONS', 'CLINIC_ACCEPTANCE_TEST'):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv('DIALOGUE_LOCAL_URL', 'http://model-test.local/v1')
    monkeypatch.setattr('clinic.module_settings.secret', lambda item: 'explicit-test-token')
    captured = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit):
            assert limit == 32769
            return json.dumps({'choices': [{'message': {'content': json.dumps(response())}}]}).encode()
    def transport(request, timeout):
        assert timeout == 8
        captured.append((request.full_url, json.loads(request.data), request.get_header('Authorization')))
        return Reply()
    monkeypatch.setattr('clinic.identity_form.open_http', transport)
    settings = SimpleNamespace(read=lambda: {'dialogue': {'enabled': True, 'provider': provider,
                                'model': 'test-small-model', 'base_url': 'https://external-test.example/v1'}})
    status, result = TemporalDialogue(DialogueModel(settings)).resolve(TEXT, today=TODAY.isoformat())
    assert status == 'proposal' and result['availability_proposal']['time_to'] == '24:00'
    assert len(captured) == 1
    url, payload, auth = captured[0]
    assert url == ('http://model-test.local/v1/chat/completions' if provider == 'local' else 'https://external-test.example/v1/chat/completions')
    assert payload['response_format']['type'] == 'json_schema'
    assert (auth is None) if provider == 'local' else auth == 'Bearer explicit-test-token'
    assert 'tools' not in payload


@pytest.mark.parametrize('spec', [{'time_from': '24:00'}, {'time_to': '24:01'}, {'time_to': '25:00'}])
def test_midnight_is_only_an_exclusive_end_of_day_boundary(spec):
    with pytest.raises(ValueError): validate(spec)


def test_real_bad_model_work_end_is_not_accepted_and_model_asks_missing_boundary():
    class WorkModel:
        calls=0
        def complete(self, task, schema):
            self.calls+=1
            if self.calls == 1:
                answer=response();answer.update(time_from='14:00',evidence='после работы до полуночи')
                return answer
            return {'question':'Во сколько у вас заканчивается рабочий день?'}
    model=WorkModel()
    status, result=TemporalDialogue(model).resolve('после работы до полуночи',today=TODAY.isoformat())
    assert status=='clarify' and result['availability_proposal'] is None and model.calls==2
    assert result['availability_question']=='Во сколько у вас заканчивается рабочий день?'


def test_new_time_clarification_does_not_drop_an_earlier_excluded_day():
    text='кроме субботы после обеда и до полуночи'
    answer=response();answer['evidence']=text
    status,result=TemporalDialogue(Model(answer)).resolve(text,parse('не в пятницу',TODAY),today=TODAY.isoformat())
    assert status=='proposal' and result['availability_proposal']['excluded_weekdays']==[4,5]

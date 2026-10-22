"""DOB confirmation must preserve the complete year in browser and telephony."""
from datetime import date
import re
from types import SimpleNamespace

import pytest

from calls.crm.registry import MockRegistry
from clinic.identity import normalize_birth_date
from clinic.identity_form import IdentityForm
from clinic.identity_service import IdentityService

TODAY = date(2026, 10, 21)
WEEKDAY = re.compile(r'\b(?:понедельник\w*|вторник\w*|сред[ауеы]|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b', re.I)
DATES = [
    ('1978-03-12', 'тысяча девятьсот семьдесят восемь'),
    ('2000-02-29', 'две тысячи'),
    ('2019-04-07', 'две тысячи девятнадцать'),
    ('1900-01-01', 'тысяча девятьсот'),
]


def assert_birth_readback(question, iso, year_words):
    assert year_words + ' года' in question
    assert not WEEKDAY.search(question), question
    # Parse the actual words heard by the caller, not a function's input flags.
    spoken = question.split('дата рождения ', 1)[1].split('?', 1)[0].split('. Это пациент', 1)[0]
    assert normalize_birth_date(spoken, today=TODAY) == date.fromisoformat(iso)


@pytest.mark.parametrize('iso,year_words', DATES)
def test_form_readback_full_birth_year_roundtrips_without_weekday(iso, year_words):
    form = IdentityForm(parts={'last_name': 'Иванов', 'first_name': 'Иван', 'middle_name': 'Иванович'}, birth_date=iso)
    question = form.readback()
    assert 'Иванов Иван Иванович' in question
    assert_birth_readback(question, iso, year_words)
    assert not form.confirmed_readback


@pytest.fixture
def service(monkeypatch):
    # The tested path is the deterministic local form and actual SQLite lookup.
    # No model/API is needed to speak or verify the already supplied DOB.
    import clinic.identity_service as module
    monkeypatch.setattr(module, 'configured', lambda: SimpleNamespace(read=lambda: {'dialogue': {'enabled': False}}))
    registry = MockRegistry()
    value = IdentityService(registry, today=lambda: TODAY)
    yield value
    registry._conn.close()


@pytest.mark.parametrize('iso,year_words', DATES)
def test_policy_confirm_reads_full_database_year_and_confirms_same_patient(service, iso, year_words):
    service.reg._x('INSERT INTO patients(id,last_name,first_name,middle_name,birth_date,oms) VALUES(?,?,?,?,?,?)',
                   (1, 'Иванов', 'Иван', 'Иванович', iso, '1234567890123456'))
    assert service.start('policy')[0] == 'name'
    assert service.use_policy('policy')[0] == 'policy'
    assert service.answer('policy', '1234567890123456')[0] == 'birth'
    stage, result = service.answer('policy', iso)
    assert stage == 'policy_confirm'
    assert_birth_readback(result['identity_question'], iso, year_words)
    assert 'patient_id' not in result
    stage, verified = service.answer('policy', '1')
    assert stage == 'ok' and verified['patient_id'] == 1
    assert verified['patient_birth_date'] == iso


def test_not_found_retry_keeps_complete_birth_year_in_readback(service):
    service.start('missing')
    assert service.answer('missing', 'Иванов Иван Иванович')[0] == 'birth'
    stage, result = service.answer('missing', '1978-03-12')
    assert stage == 'readback'
    assert 'patient_id' not in result
    stage, result = service.answer('missing', 'Да, верно')
    assert stage == 'readback'
    assert 'Карточку по этим данным найти не удалось' in result['identity_question']
    assert_birth_readback(result['identity_question'], '1978-03-12', 'тысяча девятьсот семьдесят восемь')
    assert 'patient_id' not in result

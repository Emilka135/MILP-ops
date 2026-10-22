"""Preferences survive extraction, bounded SQL selection and the final write guard."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import threading

import pytest
from calls.crm.availability import current, request_constraints
from calls.crm.registry import MockRegistry
from clinic.availability_constraints import matches, parse
from clinic.crm import ClinicCrm
from clinic.planner_store import slots

TODAY = date(2026, 10, 21)


@pytest.fixture
def clinic(tmp_path, monkeypatch):
    monkeypatch.setenv('CRM_NOW', '2026-10-21T11:00:00+04:00')
    reg = MockRegistry(str(tmp_path / 'preferences.sqlite'), now=datetime(2026, 10, 21, 11))
    doctor = reg.add_doctor('Тестов', 'Тестов Тест Тестович', 'терапевт')
    patient = reg.add_patient('Иванов', 'Иван', 'Петрович', date(1985, 4, 12))
    for day in range(22, 29):
        for clock in ('11:45', '12:00', '14:00', '16:00', '16:15'):
            reg._x('INSERT INTO slots(doctor_id,starts_at) VALUES(?,?)', (doctor, f'2026-10-{day}T{clock}'))
    crm = ClinicCrm(reg)
    yield crm, patient
    reg._conn.close()


def test_negative_friday_is_filtered_before_three_day_limit_and_includes_noon(clinic):
    crm, patient = clinic
    spec = parse('не в пятницу, после обеда', TODAY)
    status, result = slots(crm.reg, {'specialty': 'терапевт', 'patient_id': patient,
                                   'said_daypart': 'afternoon', 'availability_constraints': spec}, datetime(2026, 10, 21, 11))
    assert status == 'ok'
    assert {row['starts_at'][:10] for row in result['slots']} == {'2026-10-22', '2026-10-24', '2026-10-25'}
    assert {row['starts_at'][11:16] for row in result['slots']} == {'12:00', '14:00', '16:00'}
    assert all(matches(spec, row['starts_at']) for row in result['slots'])
    assert 'NOT IN' in result['sql'] and result['params']['av_excluded_weekdays_0'] == 4


def test_standard_fsm_slot_rpc_obeys_same_structured_window(clinic):
    crm, _ = clinic
    status, result = crm.handle('find_slots', {'specialty': 'терапевт', 'said_daypart': 'afternoon',
                                            'availability_constraints': parse('не в пятницу, после обеда', TODAY)})
    assert status == 'ok' and result['slot_date'] == '2026-10-22' and result['slot_time'] == '12:00'


def test_preference_is_rechecked_before_booking_even_if_a_slot_id_is_injected(clinic):
    crm, patient = clinic
    friday = crm.reg._q("SELECT id FROM slots WHERE starts_at='2026-10-23T14:00'")[0]['id']
    status, result = crm.handle('book', {'patient_id': patient, 'slot_id': friday, 'call_id': 'unsafe',
                                       'operation_key': 'unsafe', 'availability_constraints': parse('не в пятницу', TODAY)})
    assert status == 'error' and result['error'] == 'slot_violates_availability_constraints'
    assert not crm.reg._q('SELECT * FROM operations') and not crm.reg._q('SELECT * FROM bookings')
    assert crm.reg.slot(friday).id == friday


@pytest.mark.parametrize('spec', [
    {'dates': ["2026-10-23'); DROP TABLE patients; --"]},
    {'sql': 'DELETE FROM slots'}, {'weekdays': [True]},
    {'time_from': '25:00'}, {'time_from_inclusive': 'false'},
    {'weekdays': [4], 'excluded_weekdays': [4]},
    {'needs_clarification': True},
])
def test_ambiguous_or_malformed_filter_fails_before_registry_query(clinic, monkeypatch, spec):
    crm, _ = clinic
    monkeypatch.setattr(crm.reg, 'find_slots', lambda *_args, **_kwargs: pytest.fail('invalid filter reached SQL'))
    assert crm.handle('find_slots', {'specialty': 'терапевт', 'availability_constraints': spec})[0] == 'error'


def test_twenty_simultaneous_filters_remain_request_scoped(clinic):
    crm, _ = clinic
    barrier = threading.Barrier(20)
    def lookup(index):
        day = 22 + index % 7
        with request_constraints({'dates': [f'2026-10-{day}'], 'time_from': '14:00', 'time_to': '14:00'}):
            barrier.wait(timeout=5)
            result = crm.reg.find_slots('терапевт', limit=1)
            assert result[0].starts_at == datetime(2026, 10, day, 14)
        assert current() == {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(lookup, range(20)))


def test_real_nlu_does_not_turn_before_clock_or_birth_date_into_minimum_time(semantic_nlu):
    status, result = semantic_nlu.handle('interpret', {'text': 'Не в пятницу и до 14:00', 'today': TODAY.isoformat()})
    assert status == 'ok' and 'said_time' not in result['vars']
    spec = result['vars']['availability_constraints']
    assert spec['excluded_weekdays'] == [4] and spec['time_to'] == '14:00' and not spec['time_to_inclusive']
    _, birth = semantic_nlu.handle('interpret', {'text': '12.04.1985', 'today': TODAY.isoformat(), 'identity_field': 'birth_date'})
    assert 'availability_constraints' not in birth['vars'] and 'said_date' not in birth['vars']

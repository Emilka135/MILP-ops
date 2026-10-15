from datetime import date, datetime
from calls.crm.registry import MockRegistry, seed_demo
from clinic.crm import ClinicCrm
from clinic import hours
from clinic.inputs import load


def test_regulation_hours_and_dated_exception_use_same_database(tmp_path, monkeypatch):
    monkeypatch.setenv('CRM_NOW','2026-10-17T09:00:00+04:00')
    reg = MockRegistry(tmp_path/'clinic.sqlite', datetime(2026,10,17,9))
    crm = ClinicCrm(reg)
    assert crm.handle('meta',{})[1]['operator_available']
    assert not hours.is_open(reg, datetime(2026,10,17,15))
    assert not hours.is_open(reg, datetime(2026,10,18,12))
    calendar = tmp_path/'closed.csv'
    calendar.write_text('service,day,opens,closes,closed\nregistrar,2026-10-17,,,1\n')
    load(reg, {'CRM_SERVICE_CALENDAR':str(calendar)})
    assert not crm.handle('meta',{})[1]['operator_available']
    assert crm.handle('callback_request', {'call_id':'closed','preferred_date':'2026-10-17','preferred_time':'11:00'})[0] == 'invalid'
    assert crm.handle('callback_request', {'call_id':'open','preferred_date':'2026-10-19','preferred_time':'08:00'})[0] == 'ok'
    assert crm.handle('schedule.service',{})[1]['schedule']['source'] == 'clinic_order'


def test_robot_can_book_while_registrar_sleeps(tmp_path, monkeypatch):
    monkeypatch.setenv('CRM_NOW','2026-10-15T23:00:00+04:00')
    reg = MockRegistry(tmp_path/'clinic.sqlite', datetime(2026,10,15,23))
    seed_demo(reg,date(2026,10,16))
    crm = ClinicCrm(reg)
    assert not crm.handle('meta',{})[1]['operator_available']
    status, result = crm.handle('book', {'patient_id':3,'slot_id':1,'operation_key':'night','call_id':'night'})
    assert status == 'ok' and result['booking_id']

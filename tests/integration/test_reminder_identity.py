"""An outbound recipient must match the booking owner, beyond knowing a card number."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from calls.crm.registry import MockRegistry, seed_demo
from clinic.crm import ClinicCrm


@pytest.fixture
def appointment(monkeypatch):
    monkeypatch.setattr('clinic.identity_service.configured',lambda:SimpleNamespace(read=lambda:{'dialogue':{'enabled':False}}))
    registry=MockRegistry(':memory:',now=datetime(2026,10,23,11))
    seed_demo(registry,date(2026,10,24))
    booking=registry.book(3,1)
    yield ClinicCrm(registry),booking
    registry._conn.close()


def test_outbound_verification_rejects_another_patient_and_preserves_booking(appointment):
    crm,booking=appointment
    assert crm.handle('verify_booking_identity',{'patient_id':1,'booking_id':booking.id})[0]=='mismatch'
    assert crm.handle('verify_booking_identity',{'patient_id':3,'booking_id':booking.id})==('ok',{'booking_id':booking.id})
    crm.handle('identity_start',{'call_id':'wrong-person','locked_patient_id':3})
    person=crm.reg.patient_by_id(1)
    assert crm.handle('identity_answer',{'call_id':'wrong-person','text':person.full_name})[0]=='birth'
    assert crm.handle('identity_answer',{'call_id':'wrong-person','text':person.birth_date.isoformat()})[0]=='readback'
    status,result=crm.handle('identity_answer',{'call_id':'wrong-person','text':'Да'})
    assert status=='operator' and result['reason']=='identity_conflict'
    assert 'patient_id' not in result
    assert crm.reg.booking(booking.id).status=='booked' and len(crm.reg.execute('SELECT * FROM bookings'))==1


def test_card_digits_alone_cannot_verify_a_reminder_recipient(appointment):
    crm,booking=appointment
    crm.handle('identity_start',{'call_id':'card-only','locked_patient_id':3})
    status,result=crm.handle('identity_answer',{'call_id':'card-only','text':'1003'})
    assert status=='name' and 'patient_id' not in result and not result['identity_queries']
    assert not result['identity_readback_confirmed']
    assert crm.reg.booking(booking.id).status=='booked'

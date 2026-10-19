"""Actual SQLite scheduling and shared reservation budget; no outbound calls."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
from calls.crm.registry import MockRegistry
from clinic.crm import ClinicCrm
from clinic.outreach import due_reminders, reminder_due

AT = datetime(2026, 10, 19, 11, tzinfo=timezone(timedelta(hours=4)))


@pytest.fixture
def crm(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_NOW", AT.isoformat())
    reg = MockRegistry(str(tmp_path / "outreach.sqlite"), AT.replace(tzinfo=None))
    doctor = reg.add_doctor("Тест", "Тестовый Врач", "терапевт", "1", False)
    patient = reg.add_patient("Тест", "Пациент", "", AT.date().replace(year=1990), "12345678901", "1003", "79000000000")
    crm = ClinicCrm(reg)
    return crm, doctor, patient


def booking(fixture, days):
    crm, doctor, patient = fixture
    sid = crm.reg._x("INSERT INTO slots(doctor_id,starts_at) VALUES(?,?)", (doctor, (AT + timedelta(days=days)).replace(tzinfo=None).isoformat(timespec="minutes")))
    return crm.reg.book(patient, sid)


def history(fixture, days, count=10, doctor=None):
    crm, original, patient = fixture
    for index in range(count):
        start = AT - timedelta(days=60 + index)
        assert crm.waiting.handle("waiting.record", {
            "patient_id": patient, "doctor_id": doctor or original, "specialty": "терапевт",
            "request_at": start.isoformat(), "appointment_at": (start + timedelta(days=days)).isoformat(),
            "request_key": f"synthetic-history-{doctor}-{index}",
        }, AT)[0] == "ok"


def test_long_measured_wait_uses_seven_days_instead_of_tomorrow(crm):
    history(crm, 21)
    booked = booking(crm, 7)
    decision = reminder_due(crm[0], booked, AT)
    assert decision["due"] and decision["days"] == 7
    assert decision["basis"] == "measured_pilot_policy"
    assert decision["statistics"]["median_days"] == 21
    listed = due_reminders(crm[0], AT)
    assert listed["items"][0]["booking_id"] == booked.id
    assert listed["items"][0]["reminder"] == decision
    assert crm[0].reg.execute("SELECT * FROM dial_attempts") == []


def test_imported_booking_inside_reminder_window_catches_up(crm):
    history(crm, 21)
    assert reminder_due(crm[0], booking(crm, 4), AT)["due"]
    assert not reminder_due(crm[0], booking(crm, 8), AT)["due"]


def test_short_measured_wait_keeps_reminder_but_disables_early_offer(crm):
    history(crm, 3)
    decision = reminder_due(crm[0], booking(crm, 2), AT)
    assert decision["due"] and decision["days"] == 2
    assert decision["early_offer_enabled"] is False


def test_unknown_wait_uses_explicit_fallback_without_invented_median(crm):
    booked = booking(crm, 1)
    decision = reminder_due(crm[0], booked, AT)
    assert decision["due"] and decision["days"] == 1
    assert decision["basis"] == "policy_fallback"
    assert decision["statistics"]["median_days"] is None
    assert decision["fallback_reason"] == "waiting_time_not_measured"
    assert not reminder_due(crm[0], booking(crm, 2), AT)["due"]


def test_other_doctor_history_does_not_define_this_doctor_horizon(crm):
    other = crm[0].reg.add_doctor("Другой", "Другой Врач", "терапевт", "2", False)
    history(crm, 21, doctor=other)
    decision = reminder_due(crm[0], booking(crm, 7), AT)
    assert not decision["due"] and decision["days"] == 1


@pytest.mark.parametrize("hour,due", [(9,False),(10,True),(21,True),(22,False)])
def test_contact_window_is_clinic_gmt4(crm, hour, due):
    booked = booking(crm, 1)
    # Supply UTC time; the helper must evaluate the clinic clock.
    at = AT.replace(hour=hour).astimezone(timezone.utc)
    assert reminder_due(crm[0], booked, at)["due"] is due


def test_past_confirmed_cancelled_and_missing_phone_are_not_due(crm):
    booked = booking(crm, 1)
    data = asdict(booked)
    for patch, reason in [
        ({"starts_at": AT}, "appointment_in_past"),
        ({"confirmed": True}, "already_confirmed"),
        ({"status": "cancelled"}, "booking_not_active"),
    ]:
        result = reminder_due(crm[0], {**data, **patch}, AT)
        assert result["due"] is False and result["reason"] == reason
    crm[0].reg._x("UPDATE patients SET phone='' WHERE id=?", (booked.patient_id,))
    assert reminder_due(crm[0], booked, AT)["reason"] == "phone_missing"


def test_disabled_associated_protocol_cannot_enter_automatic_reminder(crm):
    booked = {**asdict(booking(crm, 1)), "procedure_code": "gastroscopy"}
    result = reminder_due(crm[0], booked, AT)
    assert not result["due"] and result["reason"] == "preparation_not_approved"
    assert result["medical_instructions"] is False
    assert "reminder_text_ru" not in result


def test_absent_procedure_association_does_not_infer_medical_preparation(crm):
    booked = booking(crm, 1)
    crm[0].reg._x("UPDATE doctors SET specialty='ФГДС' WHERE id=?", (booked.doctor_id,))
    result = reminder_due(crm[0], crm[0].reg.booking(booked.id), AT)
    assert result["due"]
    assert result["preparation_source"] == "not_associated_with_booking"
    assert result["medical_instructions"] is False


def test_approved_explicit_protocol_extends_fallback_horizon(crm):
    q = crm[0].waiting
    approval = {"enabled": True,"review_required": False,"clinic_id":"SYNTHETIC", "approved_by":"synthetic-clinician", "approved_at":"2026-10-01T10:00:00+04:00", "review_due_on":"2026-10-31"}
    q.preparation = {**approval,"procedures":[{**approval,"id":"test","lead_time_hours":72}]}
    result = reminder_due(crm[0], {**asdict(booking(crm, 4)), "procedure_code":"test"}, AT)
    assert result["due"] and result["days"] == 4
    assert result["basis"] == "policy_fallback"
    assert result["medical_instructions"] is False


def test_measured_policy_avoids_withdrawal_biased_cohort(crm):
    history(crm, 21)
    crm[0].reg._x("UPDATE wait_requests SET state='withdrawn' WHERE id=1")
    result = reminder_due(crm[0], booking(crm, 7), AT)
    assert result["days"] == 1 and result["basis"] == "policy_fallback"
    assert result["fallback_reason"] == "withdrawn_requests_require_cohort_review"


def test_shared_existing_phone_budget_is_not_spent_by_read_only_scheduler(crm, monkeypatch):
    c = crm[0]
    booked = booking(crm, 1)
    for clock in ("10:00", "10:20", "13:20", "13:40", "16:40", "17:00"):
        monkeypatch.setenv("CRM_NOW", f"2026-10-19T{clock}:00+04:00")
        assert due_reminders(c, datetime.fromisoformat(f"2026-10-19T{clock}:00+04:00"))["items"]
        status, result = c.handle("dial_reserve", {"booking_id":booked.id})
        assert status == "ok", result
        c.handle("dial_result", {"attempt_id":result["attempt_id"],"outcome":"no_answer"})
    monkeypatch.setenv("CRM_NOW", "2026-10-19T21:00:00+04:00")
    assert c.handle("dial_reserve", {"booking_id":booked.id})[0] == "blocked"
    assert c.reg.execute("SELECT count(*) AS n FROM dial_attempts")[0]["n"] == 6


def test_nullable_legacy_confirmation_is_still_unconfirmed(crm):
    booked = booking(crm, 1)
    crm[0].reg._x("UPDATE bookings SET confirmed=NULL WHERE id=?", (booked.id,))
    assert [item["booking_id"] for item in due_reminders(crm[0], AT)["items"]] == [booked.id]


def test_censored_wait_does_not_become_a_measured_horizon(crm):
    c, doctor, patient = crm
    for index in range(10):
        assert c.waiting.handle("waiting.record", {
            "patient_id": patient,"doctor_id": doctor,"specialty":"терапевт",
            "request_at": (AT - timedelta(days=21+index)).isoformat(),
            "request_key": f"synthetic-still-waiting-{index}",
        }, AT)[0] == "ok"
    result = reminder_due(c, booking(crm, 7), AT)
    assert result["statistics"]["status"] == "censored"
    assert result["statistics"]["median_days"] is None
    assert not result["due"] and result["days"] == 1


def test_adaptive_horizon_is_used_by_real_crm_enumeration_and_reservation(crm, monkeypatch):
    c = crm[0]
    monkeypatch.setenv("CRM_NOW", AT.isoformat())
    history(crm, 21)
    booked = booking(crm, 7)
    status, listed = c.handle("dial_due", {})
    assert status == "ok"
    item = next(item for item in listed["items"] if item["booking_id"] == booked.id)
    assert item["reminder"]["days"] == 7 and item["reminder"]["basis"] == "measured_pilot_policy"
    assert c.reg.execute("SELECT count(*) AS n FROM dial_attempts")[0]["n"] == 0
    status, reserved = c.handle("dial_reserve", {"booking_id": booked.id})
    assert status == "ok" and reserved["attempt_id"] > 0
    assert c.handle("dial_reserve", {"booking_id": booked.id})[0] == "blocked"


def test_reservation_rechecks_actual_horizon_not_browser_supplied_due_flag(crm, monkeypatch):
    c = crm[0]
    monkeypatch.setenv("CRM_NOW", AT.isoformat())
    history(crm, 21)
    booked = booking(crm, 8)
    status, denied = c.handle("dial_reserve", {"booking_id":booked.id, "reminder":{"due":True,"days":999}})
    assert status == "blocked" and denied["reason"] == "reminder_date_not_reached"
    assert c.reg.execute("SELECT count(*) AS n FROM dial_attempts")[0]["n"] == 0

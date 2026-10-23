"""Read-only reminder eligibility shared by enumeration and dial reservation.

This module never originates calls or spends the persistent per-phone attempt
budget. CRM owns that reservation. The current Booking schema has no procedure
association: without an explicit procedure_code this helper attaches no medical
instructions and makes no preparation assumptions from specialty/name.
"""

import math
from datetime import datetime, timedelta, timezone

from .queueing import reminder_days

CLINIC_ZONE = timezone(timedelta(hours=4))
MIN_SAMPLES = 10
FALLBACK_DAYS = 1


def _value(booking, name, default=None):
    return booking.get(name, default) if isinstance(booking, dict) else getattr(booking, name, default)


def _local(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return stamp.replace(tzinfo=CLINIC_ZONE) if stamp.tzinfo is None else stamp.astimezone(CLINIC_ZONE)


def reminder_due(crm, booking, at):
    """Return explainable eligibility; future bookings inside N days catch up.

    Using <= N instead of exact-day equality keeps a newly imported or rescheduled
    booking eligible after its initial reminder date. Existing CRM per-phone
    limits and successful-contact markers prevent repeated/parallel origination.
    """
    at = _local(at)
    result = {
        "due": False,
        "reason": "booking_not_due",
        "days": None,
        "basis": None,
        "statistics": None,
        "preparation_source": "not_associated_with_booking",
        "medical_instructions": False,
        "timezone": "GMT+4",
        "window": "10:00-22:00",
        "eligibility": "future_appointment_within_calendar_days",
    }
    if booking is None or _value(booking, "status") != "booked":
        return {**result, "reason": "booking_not_active"}
    if _value(booking, "confirmed"):
        return {**result, "reason": "already_confirmed"}
    try:
        appointment = _local(_value(booking, "starts_at"))
    except (TypeError, ValueError, AttributeError):
        return {**result, "reason": "invalid_appointment_time"}
    if appointment <= at:
        return {**result, "reason": "appointment_in_past"}
    doctor = crm.reg.execute(
        "SELECT specialty,active FROM doctors WHERE id=?", (_value(booking, "doctor_id"),)
    )
    if not doctor or not doctor[0]["active"]:
        return {**result, "reason": "doctor_not_active"}
    patient = crm.reg.patient_by_id(_value(booking, "patient_id"))
    if not patient or not patient.phone:
        return {**result, "reason": "phone_missing"}
    statistics = crm.waiting.stats({
        "specialty": doctor[0]["specialty"],
        "doctor_id": _value(booking, "doctor_id"),
        "min_samples": MIN_SAMPLES,
    }, at)
    result["statistics"] = statistics
    median = statistics.get("median_days")
    measured = (
        statistics.get("status") == "measured"
        and statistics.get("samples", 0) >= MIN_SAMPLES
        and statistics.get("withdrawn", 0) == 0
        and isinstance(median, (int, float))
        and not isinstance(median, bool)
        and math.isfinite(median)
        and median >= 0
    )
    prep_hours = 0
    procedure = _value(booking, "procedure_code")
    if procedure:
        result["preparation_source"] = "explicit_booking_procedure_code"
        status, protocol = crm.waiting.handle(
            "preparation.lookup", {"procedure_code": procedure}, at
        )
        if status != "ok":
            return {**result, "reason": "preparation_not_approved",
                    "preparation_reason": protocol.get("reason", "unknown_procedure")}
        try:
            prep_hours = float(protocol["lead_time_hours"])
        except (KeyError, ValueError, TypeError):
            return {**result, "reason": "invalid_preparation_lead_time"}
        if not math.isfinite(prep_hours) or prep_hours < 0:
            return {**result, "reason": "invalid_preparation_lead_time"}
        # Eligibility is not proof of the patient's preparation. The caller
        # must separately verify any individual plan before making an offer.
        result["approved_procedure_code"] = procedure
    if measured:
        policy = reminder_days(statistics, prep_hours=prep_hours)
        days = policy["days"]
        result.update(basis=policy["basis"], early_offer_enabled=policy["early_offer_enabled"])
    else:
        days = max(FALLBACK_DAYS, math.ceil(prep_hours / 24) + 1 if prep_hours else 0)
        result.update(
            basis="policy_fallback",
            fallback_reason="withdrawn_requests_require_cohort_review"
            if statistics.get("withdrawn", 0) else "waiting_time_not_measured",
            early_offer_enabled=False,
        )
    result.update(days=days, days_until_appointment=(appointment.date() - at.date()).days,
                  reminder_on=(appointment.date() - timedelta(days=days)).isoformat())
    if not 10 <= at.hour < 22:
        return {**result, "reason": "outside_contact_hours"}
    if at.date() < appointment.date() - timedelta(days=days):
        return {**result, "reason": "reminder_date_not_reached"}
    return {**result, "due": True, "reason": "reminder_due"}


def due_reminders(crm, at):
    """Enumerate actual active bookings without calling a network or reserving."""
    at = _local(at)
    items = []
    checked = 0
    for row in crm.reg.execute("SELECT id FROM bookings WHERE status='booked' AND COALESCE(confirmed,0)=0 ORDER BY starts_at,id"):
        booking = crm.reg.booking(row["id"])
        decision = reminder_due(crm, booking, at)
        checked += 1
        if decision["due"]:
            patient = crm.reg.patient_by_id(booking.patient_id)
            items.append({"booking_id": booking.id, "phone": patient.phone, "reminder": decision})
    return {
        "items": items,
        "checked": checked,
        "checked_at": at.isoformat(),
        "source": "bookings+doctor_wait_requests",
        "fallback_days": FALLBACK_DAYS,
        "contact_hours": "10:00-22:00 GMT+4",
        "attempt_budget": "reserved_separately_by_crm_per_phone",
        "preparation_association": "not_present_in_current_booking_schema",
    }

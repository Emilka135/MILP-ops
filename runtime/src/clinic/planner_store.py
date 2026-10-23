"""Approved read-only slot query; model output is never accepted as SQL."""

import json
import re
from datetime import date, timedelta, timezone

from calls.crm.registry import DAYPART_WINDOWS

SCHEMA = """CREATE TABLE IF NOT EXISTS patient_assignments(patient_id INTEGER NOT NULL, specialty TEXT NOT NULL, doctor_id INTEGER NOT NULL, PRIMARY KEY(patient_id,specialty));"""
MAX_EXCLUDED = 100
SLOTS_PER_DAY = 32
CLINIC_ZONE = timezone(timedelta(hours=4))
SQL = """WITH eligible AS (
 SELECT s.id,s.starts_at,d.id AS doctor_id,d.full_name AS doctor,d.specialty,d.cabinet,
 substr(s.starts_at,1,10) AS day
 FROM slots s JOIN doctors d ON d.id=s.doctor_id
 WHERE s.status='free' AND d.active=1 AND d.specialty=:specialty AND s.starts_at>:now
 AND (:doctor IS NULL OR d.id=:doctor)
 AND (d.needs_referral=0 OR :referral_verified=1)
 AND (:day IS NULL OR substr(s.starts_at,1,10)>=:day)
 AND (:part_start IS NULL OR (substr(s.starts_at,12,5)>=:part_start AND substr(s.starts_at,12,5)<:part_end))
 AND (:time IS NULL OR substr(s.starts_at,12,5)>=:time)
 AND NOT EXISTS (SELECT 1 FROM json_each(:excluded) x WHERE x.value=s.id)
), days AS (SELECT DISTINCT day FROM eligible ORDER BY day LIMIT 3),
ranked AS (
 SELECT e.*,row_number() OVER (PARTITION BY e.day ORDER BY e.starts_at,e.id) AS day_rank
 FROM eligible e JOIN days ON days.day=e.day
)
SELECT id,starts_at,doctor_id,doctor,specialty,cabinet FROM ranked
WHERE day_rank<=:slots_per_day ORDER BY starts_at,id"""


def normalize_constraints(args):
    """Only canonical scheduling fields may be forwarded to the slot chooser."""
    if not isinstance(args, dict):
        raise ValueError("invalid_constraints")
    specialty = args.get("specialty")
    if not isinstance(specialty, str) or not specialty.strip() or len(specialty) > 100:
        raise ValueError("invalid_specialty")
    day = args.get("said_date") or None
    if day is not None:
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("invalid_date")
        day = date.fromisoformat(day).isoformat()
    clock = args.get("said_time") or None
    if clock is not None and (
        not isinstance(clock, str)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock)
    ):
        raise ValueError("invalid_time")
    part = args.get("said_daypart") or None
    if part not in {None, *DAYPART_WINDOWS}:
        raise ValueError("invalid_daypart")
    excluded = args.get("excluded") or []
    if not isinstance(excluded, list) or len(excluded) > MAX_EXCLUDED:
        raise ValueError("invalid_excluded")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in excluded
    ):
        raise ValueError("invalid_excluded")
    from .availability_constraints import validate
    availability = validate(args.get('availability_constraints'))
    return {
        **({'availability_constraints': availability} if args.get('availability_constraints') else {}),
        "specialty": specialty.strip(),
        "said_date": day,
        "said_time": clock,
        "said_daypart": part,
        "excluded": list(dict.fromkeys(excluded)),
    }


def slots(reg, args, now):
    try:
        normalized = normalize_constraints(args)
        raw_patient = args.get("patient_id")
        if isinstance(raw_patient, bool) or not isinstance(raw_patient, (int, str)):
            raise TypeError("invalid_patient")
        if isinstance(raw_patient, str) and not re.fullmatch(r"[1-9]\d*", raw_patient):
            raise ValueError("invalid_patient")
        patient = int(raw_patient)
        if patient <= 0:
            raise ValueError("invalid_patient")
    except (TypeError, ValueError):
        return "error", {"error": "invalid_slot_constraints"}
    if not reg.patient_by_id(patient):
        return "error", {"error": "patient_required"}
    specialty = normalized["specialty"]
    assigned = reg.execute(
        "SELECT doctor_id FROM patient_assignments WHERE patient_id=? AND specialty=?",
        (patient, specialty),
    )
    doctor = assigned[0]["doctor_id"] if assigned else None
    doctors = reg.execute(
        "SELECT needs_referral FROM doctors WHERE active=1 AND specialty=? AND (? IS NULL OR id=?)",
        (specialty, doctor, doctor),
    )
    # This flag is a trusted internal attestation, not patient-reported direction.
    verified = args.get("referral_verified") is True
    if doctors and all(row["needs_referral"] for row in doctors) and not verified:
        return "error", {
            "error": "referral_must_be_verified",
            "policy_source": "doctors.needs_referral",
        }
    at = (
        now.replace(tzinfo=CLINIC_ZONE)
        if now.tzinfo is None
        else now.astimezone(CLINIC_ZONE)
    )
    availability = normalized.get('availability_constraints', {})
    window = DAYPART_WINDOWS.get(normalized["said_daypart"])
    if availability.get('time_from') is not None or availability.get('time_to') is not None:
        window = None
    params = {
        "specialty": specialty,
        "now": at.replace(tzinfo=None).isoformat(timespec="minutes"),
        "doctor": doctor,
        "day": normalized["said_date"],
        "time": normalized["said_time"],
        "part_start": window[0] if window else None,
        "part_end": window[1] if window else None,
        "referral_verified": int(verified),
        "excluded": json.dumps(normalized["excluded"]),
        "slots_per_day": SLOTS_PER_DAY,
    }
    from .availability_constraints import sql_filter
    clause, bound = sql_filter(availability)
    query = SQL if clause == '1=1' else SQL.replace(' AND NOT EXISTS', ' AND (' + clause + ')\n AND NOT EXISTS', 1)
    params.update(bound)
    return "ok", {
        "slots": reg.execute(query, params),
        "sql": query,
        "params": params,
        "max_days": 3,
        "max_slots_per_day": SLOTS_PER_DAY,
        "date_constraint": "on_or_after",
        "referral_policy_source": "doctors.needs_referral",
        "assignment_source": "patient_assignments"
        if assigned
        else "unassigned_specialty_search",
    }

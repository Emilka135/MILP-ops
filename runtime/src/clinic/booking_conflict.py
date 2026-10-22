"""Server-side duplicate protection; consent is scoped to the exact future visits."""
import hashlib
import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS booking_consents(
 call_id TEXT NOT NULL, patient_id INTEGER NOT NULL, specialty TEXT NOT NULL,
 fingerprint TEXT NOT NULL, PRIMARY KEY(call_id,patient_id,specialty));
"""


def upcoming(crm, patient_id, specialty, at):
    from .specialty_dictionary import canonical_specialty
    specialty = canonical_specialty(specialty)
    rows = crm.reg._q(
        "SELECT b.id FROM bookings b JOIN doctors d ON d.id=b.doctor_id "
        "WHERE b.patient_id=? AND b.status='booked' AND b.starts_at>? ORDER BY b.starts_at,b.id",
        (int(patient_id), at.replace(tzinfo=None).isoformat(timespec="minutes")),
    )
    return [b for row in rows if (b := crm.reg.booking(row["id"]))
            and canonical_specialty(b.specialty) == specialty]


def fingerprint(visits):
    return hashlib.sha256(json.dumps([(v.id, v.slot_id, v.starts_at.isoformat()) for v in visits]).encode()).hexdigest()


def check(crm, args, at, *, allow_consent=False):
    from .specialty_dictionary import canonical_specialty
    specialty = canonical_specialty(str(args["specialty"]))
    visits = upcoming(crm, args["patient_id"], specialty, at)
    if not visits:
        return "clear", {}
    call_id = str(args.get("call_id") or "")
    stamp = fingerprint(visits)
    if args.get("decision") == "allow_new":
        if not call_id:
            return "invalid", {"error": "call_id_required"}
        crm.reg._x("INSERT OR REPLACE INTO booking_consents VALUES(?,?,?,?)",
                   (call_id, int(args["patient_id"]), specialty, stamp))
        return "clear", {}
    if allow_consent and call_id and crm.reg._q(
        "SELECT 1 FROM booking_consents WHERE call_id=? AND patient_id=? AND specialty=? AND fingerprint=?",
        (call_id, int(args["patient_id"]), specialty, stamp),
    ):
        return "clear", {}
    # The question names the nearest visit; alternatives never cancel it implicitly.
    payload = crm.module._booking_payload(visits[0])
    payload.update(conflict_count=len(visits), conflict_booking_ids=[v.id for v in visits])
    return "conflict", payload

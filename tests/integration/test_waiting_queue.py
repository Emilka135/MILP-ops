"""Durable backfill offers and clinical approval gates; no external calls or audio."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from calls.crm.registry import MockRegistry
from clinic.crm import ClinicCrm
from clinic.queueing import OFFER_TTL_SECONDS, waiting_estimate

from app.mis.mock_stand import MockStand
from app.types import MisError, MisTimeout

AT = datetime(2026, 10, 16, 10, tzinfo=timezone(timedelta(hours=4)))


class LocalStand:
    """Actual in-process stand contract, with explicit error injection."""

    def __init__(self, path, doctor):
        self.mock = MockStand(path, seed=False)
        self.mock.data["doctors"] = [
            {
                "id": doctor,
                "full_name": "Врач Тестовый",
                "specialty": "терапевт",
                "is_active": True,
            }
        ]
        self.calls = []
        self.failure = None

    def request(self, method, path, body=None, query=None):
        self.calls.append((method, path, body))
        if self.failure == "denied" and path.startswith("/visits"):
            raise MisError("denied", 403, "DENIED")
        response = self.mock.handle(method, path, query, body)[1]
        if self.failure == "timeout" and path.startswith("/visits"):
            raise MisTimeout("lost acknowledgement", 0, "TIMEOUT")
        return response


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_NOW", AT.isoformat())
    reg = MockRegistry(str(tmp_path / "queue.sqlite"), AT.replace(tzinfo=None))
    doctor = reg.add_doctor("Тестовый", "Врач Тестовый", "терапевт", "1", False)
    for patient in range(1, 4):
        reg.add_patient(
            f"Пациент{patient}",
            "Тест",
            "",
            datetime(1990, 1, patient).date(),
            str(patient).zfill(11),
            str(patient),
            str(79000000000 + patient),
        )
    # Three individual slots with explicit dates: target, nearest existing, later existing.
    for stamp in ("2026-10-17T10:00", "2026-10-23T10:00", "2026-10-25T10:00"):
        reg._x("INSERT INTO slots(doctor_id,starts_at) VALUES(?,?)", (doctor, stamp))
    stand = LocalStand(tmp_path / "stand.json", doctor)
    crm = ClinicCrm(reg, stand)
    for patient, slot in ((1, 2), (2, 3)):
        assert (
            crm.handle(
                "book",
                {
                    "patient_id": patient,
                    "slot_id": slot,
                    "operation_key": f"seed:{patient}",
                },
            )[0]
            == "ok"
        )
    for index in range(10):
        request_at = AT.replace(day=1, month=9) + timedelta(days=index)
        assert (
            crm.waiting.handle(
                "waiting.record",
                {
                    "patient_id": 1,
                    "doctor_id": doctor,
                    "specialty": "терапевт",
                    "request_at": request_at.isoformat(),
                    "appointment_at": (request_at + timedelta(days=21)).isoformat(),
                    "source": f"synthetic:{index}",
                },
                AT,
            )[0]
            == "ok"
        )
    for patient in range(1, 4):
        assert (
            crm.waiting.handle(
                "waiting.join",
                {
                    "patient_id": patient,
                    "doctor_id": doctor,
                    "specialty": "терапевт",
                    "opt_in": True,
                },
                AT,
            )[0]
            == "ok"
        )
    assert (
        crm.waiting.handle(
            "waiting.record",
            {
                "patient_id": 3,
                "doctor_id": doctor,
                "specialty": "терапевт",
                "request_at": "2026-09-30T10:00:00+04:00",
            },
            AT,
        )[0]
        == "ok"
    )
    return crm, stand


def approved_catalog(q):
    approval = {
        "enabled": True,
        "review_required": False,
        "clinic_id": "synthetic-clinic",
        "approved_by": "synthetic-clinician",
        "approved_at": "2026-10-01T10:00:00+04:00",
        "review_due_on": "2026-10-31",
    }
    q.preparation = {
        **approval,
        "procedures": [
            {
                **approval,
                "id": "test-procedure",
                "lead_time_hours": 12,
                "requires_individual_plan": True,
                "reminder_text_ru": "Синтетическая утверждённая памятка",
            }
        ],
    }
    return q.preparation


def next_offer(q, at=AT):
    status, offer = q.handle("backfill.next", {"slot_id": 1}, at)
    assert status == "ok", (status, offer)
    return offer


def accept(q, offer, at=AT):
    return q.handle(
        "backfill.accept",
        {"offer_id": offer["offer_id"], "verified_patient_id": offer["patient_id"]},
        at,
    )


def test_shipped_research_catalog_never_dispatches_disabled_text(queue):
    q = queue[0].waiting
    assert q.preparation["enabled"] is False
    for procedure in q.preparation["procedures"]:
        status, result = q.handle(
            "preparation.lookup", {"procedure_code": procedure["id"]}, AT
        )
        assert status == "blocked"
        assert "reminder_text_ru" not in result
        assert (
            q.preparation_ok(procedure["id"], AT + timedelta(days=10), AT, True)[0]
            is False
        )


@pytest.mark.parametrize(
    "record,field,value,reason",
    [
        ("catalog", "enabled", False, "clinic_review_required"),
        ("catalog", "review_required", True, "clinic_review_required"),
        ("catalog", "approved_by", None, "clinic_approval_missing"),
        ("catalog", "review_due_on", "2026-10-15", "clinic_approval_expired"),
        (
            "catalog",
            "approved_at",
            "2026-10-17T00:00:00+04:00",
            "clinic_approval_not_effective",
        ),
        ("procedure", "enabled", False, "clinic_review_required"),
        ("procedure", "review_due_on", "2026-10-15", "clinic_approval_expired"),
        ("procedure", "review_due_on", "not-a-date", "invalid_clinic_approval"),
        ("procedure", "clinic_id", "another-clinic", "clinic_approval_mismatch"),
    ],
)
def test_both_catalog_and_procedure_require_current_clinical_approval(
    queue, record, field, value, reason
):
    q = queue[0].waiting
    catalog = approved_catalog(q)
    (catalog if record == "catalog" else catalog["procedures"][0])[field] = value
    assert q.preparation_ok("test-procedure", AT + timedelta(days=4), AT, True) == (
        False,
        reason,
    )
    status, item = q.handle(
        "preparation.lookup", {"procedure_code": "test-procedure"}, AT
    )
    assert status == "blocked" and "reminder_text_ru" not in item


def test_confirmation_cannot_override_short_notice_or_missing_individual_plan(queue):
    q = queue[0].waiting
    approved_catalog(q)
    assert q.preparation_ok("test-procedure", AT + timedelta(hours=2), AT, True) == (
        False,
        "insufficient_preparation_time",
    )
    assert q.preparation_ok("test-procedure", AT + timedelta(days=4), AT, False) == (
        False,
        "individual_preparation_not_confirmed",
    )
    assert q.preparation_ok("test-procedure", AT + timedelta(days=4), AT, True) == (
        True,
        "approved_protocol",
    )


@pytest.mark.parametrize("lead", [None, "unknown", -1, float("nan"), float("inf")])
def test_invalid_preparation_intervals_do_not_allow_automatic_invitation(queue, lead):
    q = queue[0].waiting
    catalog = approved_catalog(q)
    catalog["procedures"][0]["lead_time_hours"] = lead
    assert q.preparation_ok("test-procedure", AT + timedelta(days=4), AT, True) == (
        False,
        "invalid_preparation_lead_time",
    )


def test_pending_retry_and_second_connection_return_identical_offer_id(queue):
    crm, _ = queue
    first = next_offer(crm.waiting)
    assert next_offer(crm.waiting) == first
    restarted = ClinicCrm(
        MockRegistry(crm.reg.path, AT.replace(tzinfo=None)), crm.stand
    )
    assert next_offer(restarted.waiting) == first
    assert first["patient_id"] == 1
    assert crm.reg.execute("SELECT count(*) AS n FROM backfill_offers")[0]["n"] == 1


def test_concurrent_owners_create_one_durable_offer(queue):
    crm, _ = queue
    other = ClinicCrm(MockRegistry(crm.reg.path, AT.replace(tzinfo=None)), crm.stand)
    barrier = threading.Barrier(16)

    def run(index):
        barrier.wait(timeout=5)
        return next_offer((crm if index % 2 else other).waiting)

    with ThreadPoolExecutor(max_workers=16) as workers:
        offers = list(workers.map(run, range(16)))
    assert len({offer["offer_id"] for offer in offers}) == 1
    assert (
        crm.reg.execute(
            "SELECT count(*) AS n FROM backfill_offers WHERE state='pending'"
        )[0]["n"]
        == 1
    )


def test_expiry_rejects_old_acceptance_and_advances_to_next_patient(queue):
    crm, stand = queue
    first = next_offer(crm.waiting)
    calls_before = len(stand.calls)
    expired_at = AT + timedelta(seconds=OFFER_TTL_SECONDS)
    assert accept(crm.waiting, first, expired_at)[0] == "error"
    assert len(stand.calls) == calls_before
    second = next_offer(crm.waiting, expired_at)
    assert second["patient_id"] == 2 and second["offer_id"] != first["offer_id"]
    assert crm.reg.booking(first["old_booking_id"]).slot_id == 2


def test_decline_keeps_original_and_selects_booked_before_unbooked(queue):
    crm, stand = queue
    first = next_offer(crm.waiting)
    old_before = crm.reg.booking(first["old_booking_id"])
    calls_before = len(stand.calls)
    args = {"offer_id": first["offer_id"]}
    assert crm.waiting.handle("backfill.decline", args, AT) == crm.waiting.handle(
        "backfill.decline", args, AT
    )
    assert crm.reg.booking(first["old_booking_id"]) == old_before
    assert len(stand.calls) == calls_before
    second = next_offer(crm.waiting)
    assert second["patient_id"] == 2
    crm.waiting.handle("backfill.decline", {"offer_id": second["offer_id"]}, AT)
    assert next_offer(crm.waiting)["patient_id"] == 3


def test_identity_consent_and_preparation_are_rechecked_before_accept(queue):
    crm, stand = queue
    q = crm.waiting
    first = next_offer(q)
    before = len(stand.calls)
    assert (
        q.handle(
            "backfill.accept",
            {"offer_id": first["offer_id"], "verified_patient_id": 999},
            AT,
        )[1]["error"]
        == "identity_required"
    )
    q.handle(
        "waiting.join", {"patient_id": 1, "specialty": "терапевт", "opt_in": False}, AT
    )
    assert accept(q, first) == ("blocked", {"reason": "consent_withdrawn"})
    assert len(stand.calls) == before

    second = next_offer(q)
    q.handle(
        "waiting.join",
        {
            "patient_id": 2,
            "specialty": "терапевт",
            "opt_in": True,
            "procedure_code": "gastroscopy",
            "prep_confirmed": True,
        },
        AT,
    )
    assert accept(q, second)[0] == "blocked"
    assert len(stand.calls) == before


def test_protocol_expiring_after_offer_is_rechecked_before_accept(queue):
    crm, stand = queue
    q = crm.waiting
    catalog = approved_catalog(q)
    for record in (catalog, catalog["procedures"][0]):
        record["review_due_on"] = "2026-10-16"
    catalog["procedures"][0]["lead_time_hours"] = 1
    q.handle(
        "waiting.join",
        {
            "patient_id": 1,
            "specialty": "терапевт",
            "opt_in": True,
            "procedure_code": "test-procedure",
            "prep_confirmed": True,
        },
        AT,
    )
    before_midnight = AT.replace(hour=23, minute=59)
    offer = next_offer(q, before_midnight)
    calls_before = len(stand.calls)
    assert accept(q, offer, before_midnight + timedelta(minutes=2)) == (
        "blocked",
        {"reason": "clinic_approval_expired"},
    )
    assert len(stand.calls) == calls_before
    assert crm.reg.booking(offer["old_booking_id"]).slot_id == 2


def test_legacy_duplicate_pending_offers_are_quarantined_on_startup(queue):
    crm, _ = queue
    crm.reg._x("DROP INDEX ix_backfill_one_pending")
    for patient in (1, 2):
        crm.reg._x(
            "INSERT INTO backfill_offers(slot_id,patient_id,created_at) VALUES(1,?,?)",
            (patient, AT.isoformat()),
        )
    restarted = ClinicCrm(
        MockRegistry(crm.reg.path, AT.replace(tzinfo=None)), crm.stand
    )
    status, result = restarted.waiting.handle("backfill.next", {"slot_id": 1}, AT)
    assert status == "blocked" and result["reason"] == "reconciliation_required"
    assert {
        row["state"] for row in crm.reg.execute("SELECT state FROM backfill_offers")
    } == {"uncertain"}


def test_other_booking_taking_target_never_cancels_old_booking(queue):
    crm, stand = queue
    offer = next_offer(crm.waiting)
    old = crm.reg.booking(offer["old_booking_id"])
    assert (
        crm.handle(
            "book", {"patient_id": 3, "slot_id": 1, "operation_key": "another-channel"}
        )[0]
        == "ok"
    )
    calls_before = len(stand.calls)
    assert accept(crm.waiting, offer) == ("blocked", {"reason": "slot_unavailable"})
    assert len(stand.calls) == calls_before
    assert crm.reg.booking(old.id) == old


@pytest.mark.parametrize(
    "failure,expected", [("denied", "failed"), ("timeout", "uncertain")]
)
def test_failed_or_uncertain_mis_move_never_releases_previous_booking(
    queue, failure, expected
):
    crm, stand = queue
    offer = next_offer(crm.waiting)
    old = crm.reg.booking(offer["old_booking_id"])
    stand.failure = failure
    assert accept(crm.waiting, offer)[0] == "error"
    assert crm.reg.booking(old.id) == old
    assert (
        crm.reg.execute("SELECT status FROM slots WHERE id=?", (old.slot_id,))[0][
            "status"
        ]
        == "booked"
    )
    assert (
        crm.reg.execute(
            "SELECT state FROM backfill_offers WHERE id=?", (offer["offer_id"],)
        )[0]["state"]
        == expected
    )
    if expected == "uncertain":
        assert crm.waiting.handle(
            "backfill.next", {"slot_id": 1}, AT + timedelta(hours=1)
        ) == (
            "blocked",
            {"offer_id": offer["offer_id"], "reason": "reconciliation_required"},
        )


def test_accept_is_idempotent_and_stops_campaign(queue):
    crm, stand = queue
    offer = next_offer(crm.waiting)
    result = accept(crm.waiting, offer)
    assert result[0] == "ok"
    writes = len(stand.calls)
    assert accept(crm.waiting, offer) == result
    assert len(stand.calls) == writes
    assert crm.reg.booking(offer["old_booking_id"]).slot_id == 1
    assert crm.reg.execute("SELECT status FROM slots WHERE id=2")[0]["status"] == "free"
    assert crm.waiting.handle("backfill.next", {"slot_id": 1}, AT)[0] == "complete"


def test_accept_in_progress_blocks_duplicate_and_expiry(queue, monkeypatch):
    crm, stand = queue
    q = crm.waiting
    offer = next_offer(q)
    started, release = threading.Event(), threading.Event()
    original = crm.handle

    def slow(op, args):
        if op == "reschedule":
            started.set()
            assert release.wait(timeout=5)
        return original(op, args)

    monkeypatch.setattr(crm, "handle", slow)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(accept, q, offer)
        try:
            assert started.wait(timeout=5)
            assert accept(q, offer)[0] == "in_progress"
            assert (
                q.handle("backfill.decline", {"offer_id": offer["offer_id"]}, AT)[0]
                == "error"
            )
            assert (
                q.handle("backfill.next", {"slot_id": 1}, AT + timedelta(hours=1))[0]
                == "in_progress"
            )
        finally:
            release.set()
        assert pending.result(timeout=5)[0] == "ok"
    assert sum(method == "PATCH" for method, _, _ in stand.calls) == 1


def test_future_appointment_is_not_counted_as_already_observed_event():
    rows = [
        {
            "request_at": "2026-10-01T00:00:00+04:00",
            "appointment_at": "2026-11-01T00:00:00+04:00",
        }
    ] * 10
    stats = waiting_estimate(rows, AT)
    assert stats["status"] == "censored"
    assert stats["median_days"] is None and stats["p75_days"] is None
    # Equivalent instants in different offsets do not create fictitious waits.
    same = [
        {
            "request_at": "2026-10-01T10:00:00+04:00",
            "appointment_at": "2026-10-01T06:00:00+00:00",
        }
    ] * 10
    assert waiting_estimate(same, AT)["median_days"] == 0


def test_completed_history_is_not_an_active_waiting_request(queue):
    crm, _ = queue
    crm.reg._x("UPDATE wait_requests SET state='withdrawn' WHERE patient_id=3")
    status, _ = crm.waiting.handle(
        "waiting.record",
        {
            "patient_id": 3,
            "doctor_id": 1,
            "specialty": "терапевт",
            "request_at": "2026-09-01T10:00:00+04:00",
            "appointment_at": "2026-09-22T10:00:00+04:00",
        },
        AT,
    )
    assert status == "ok"
    assert crm.reg.execute("SELECT state FROM wait_requests WHERE patient_id=3 ORDER BY id DESC LIMIT 1")[0]["state"] == "fulfilled"
    result = crm.waiting.candidates({"slot_id": 1}, AT)[1]
    assert 3 not in {item["patient_id"] for item in result["candidates"]}


def test_wait_for_a_specific_other_doctor_is_not_reassigned(queue):
    crm, _ = queue
    other = crm.reg.add_doctor("Другой", "Другой Врач", "терапевт", "2", False)
    crm.reg._x("UPDATE wait_requests SET doctor_id=? WHERE patient_id=3", (other,))
    result = crm.waiting.candidates({"slot_id": 1}, AT)[1]
    assert [item["patient_id"] for item in result["candidates"]] == [1, 2]


def test_existing_earlier_visit_excludes_patient_even_with_pending_request(queue):
    crm, _ = queue
    crm.reg._x("UPDATE bookings SET starts_at='2026-10-16T12:00' WHERE patient_id=1")
    result = crm.waiting.candidates({"slot_id": 1}, AT)[1]
    assert [item["patient_id"] for item in result["candidates"]] == [2, 3]


def test_candidate_order_uses_instants_not_timestamp_text(queue):
    crm, _ = queue
    crm.reg._x("UPDATE bookings SET starts_at='2026-10-23T10:00:00+14:00' WHERE patient_id=1")
    crm.reg._x("UPDATE bookings SET starts_at='2026-10-22T23:00:00-12:00' WHERE patient_id=2")
    result = crm.waiting.candidates({"slot_id": 1}, AT)[1]
    assert [item["patient_id"] for item in result["candidates"]] == [1, 2, 3]
    assert result["candidates"][0]["gain_days"] < result["candidates"][1]["gain_days"]


def test_satisfied_request_is_rechecked_before_acceptance(queue):
    crm, stand = queue
    q = crm.waiting
    for _ in range(2):
        offer = next_offer(q)
        assert q.handle("backfill.decline", {"offer_id": offer["offer_id"]}, AT)[0] == "ok"
    offer = next_offer(q)
    assert offer["patient_id"] == 3
    crm.reg._x("UPDATE wait_requests SET state='withdrawn' WHERE patient_id=3")
    before = len(stand.calls)
    assert accept(q, offer) == ("blocked", {"reason": "waiting_request_changed"})
    assert len(stand.calls) == before


def test_acceptance_records_observable_wait_and_preserves_other_doctor_request(queue):
    crm, _ = queue
    q = crm.waiting
    other = crm.reg.add_doctor("Другой", "Другой Врач", "терапевт", "2", False)
    assert q.handle("waiting.record", {
        "patient_id": 3, "doctor_id": other, "specialty": "терапевт",
        "request_at": "2026-09-29T10:00:00+04:00",
    }, AT)[0] == "ok"
    for _ in range(2):
        offer = next_offer(q)
        assert q.handle("backfill.decline", {"offer_id": offer["offer_id"]}, AT)[0] == "ok"
    offer = next_offer(q)
    assert offer["patient_id"] == 3 and accept(q, offer)[0] == "ok"
    rows = crm.reg.execute("SELECT * FROM wait_requests WHERE patient_id=3 ORDER BY id")
    assert rows[0]["state"] == "scheduled" and rows[0]["appointment_at"]
    assert rows[1]["state"] == "pending" and rows[1]["appointment_at"] is None
    # An upcoming appointment remains censored until it actually takes place.
    assert waiting_estimate([rows[0]], AT, min_samples=1)["status"] == "censored"
    observed = waiting_estimate([rows[0]], AT + timedelta(days=2), min_samples=1)
    assert observed["status"] == "measured" and observed["median_days"] == 17


@pytest.mark.parametrize("confirmation", ["false", "true", 1, None])
def test_preparation_confirmation_requires_actual_boolean(queue, confirmation):
    crm, _ = queue
    q = crm.waiting
    approved_catalog(q)
    assert q.handle("waiting.join", {
        "patient_id": 1, "specialty": "терапевт", "opt_in": True,
        "procedure_code": "test-procedure", "prep_confirmed": confirmation,
    }, AT)[0] == "ok"
    result = q.candidates({"slot_id": 1}, AT)[1]
    assert 1 not in {item["patient_id"] for item in result["candidates"]}


def test_future_request_is_not_recorded(queue):
    crm, _ = queue
    before = crm.reg.execute("SELECT count(*) AS n FROM wait_requests")[0]["n"]
    assert crm.waiting.handle("waiting.record", {
        "patient_id": 3, "specialty": "терапевт",
        "request_at": (AT + timedelta(seconds=1)).isoformat(),
    }, AT) == ("error", {"error": "request_in_future"})
    assert crm.reg.execute("SELECT count(*) AS n FROM wait_requests")[0]["n"] == before


def test_short_individual_gain_does_not_skip_nearest_patient_in_long_queue(queue):
    crm, _ = queue
    # Mean/median specialty wait is long; a one-day improvement still goes to
    # the nearest booked patient before people booked later or not booked yet.
    crm.reg._x("UPDATE bookings SET starts_at='2026-10-18T10:00' WHERE patient_id=1")
    offer = next_offer(crm.waiting)
    assert offer["patient_id"] == 1
    assert accept(crm.waiting, offer)[0] == "ok"


def test_repeated_request_event_does_not_inflate_wait_statistics(queue):
    crm, _ = queue
    args = {
        "request_key": "synthetic:patient-3-initial-request",
        "patient_id": 3, "doctor_id": 1, "specialty": "терапевт",
        "request_at": "2026-09-01T10:00:00+04:00",
        "appointment_at": "2026-09-10T10:00:00+04:00",
        "source": "synthetic-import",
    }
    before = crm.waiting.stats({"specialty": "терапевт"}, AT)["samples"]
    first = crm.waiting.handle("waiting.record", args, AT)
    again = crm.waiting.handle("waiting.record", {
        **args, "request_at": "2026-09-01T06:00:00+00:00",
    }, AT)
    assert first[0] == again[0] == "ok"
    assert first[1]["recorded"] is True and again[1]["recorded"] is False
    assert first[1]["request_id"] == again[1]["request_id"]
    assert crm.waiting.stats({"specialty": "терапевт"}, AT)["samples"] == before + 1
    assert crm.waiting.handle("waiting.record", {
        **args, "patient_id": 2,
    }, AT) == ("error", {"error": "request_key_conflict"})


@pytest.mark.parametrize("key", ["", " ", 123, "x" * 129])
def test_invalid_request_event_keys_do_not_write_observations(queue, key):
    crm, _ = queue
    assert crm.waiting.handle("waiting.record", {
        "request_key": key, "patient_id": 3, "specialty": "терапевт",
        "request_at": "2026-09-01T10:00:00+04:00",
    }, AT) == ("error", {"error": "invalid_request_key"})


def test_reopened_filled_slot_creates_new_campaign_preserving_old_result(queue):
    crm, stand = queue
    q = crm.waiting
    first = next_offer(q)
    result = accept(q, first)
    assert result[0] == "ok" and first["campaign"] == 0
    # A real subsequent cancellation frees that same slot.
    assert crm.handle("cancel", {
        "patient_id": first["patient_id"], "booking_id": first["old_booking_id"],
        "operation_key": "synthetic-later-cancellation",
    })[0] == "ok"
    second = next_offer(q)
    assert second["campaign"] == 1 and second["patient_id"] == 2
    assert second["offer_id"] != first["offer_id"]
    before = len(stand.calls)
    assert accept(q, first) == result
    assert len(stand.calls) == before
    assert next_offer(q) == second
    assert crm.reg.execute("SELECT state FROM backfill_offers WHERE id=?", (first["offer_id"],))[0]["state"] == "accepted"


def test_withdrawal_requires_identity_is_idempotent_and_keeps_observation(queue):
    crm, _ = queue
    q = crm.waiting
    request = crm.reg.execute("SELECT id FROM wait_requests WHERE patient_id=3")[0]["id"]
    assert q.handle("waiting.withdraw", {"request_id": request, "verified_patient_id": 2}, AT) == ("error", {"error": "identity_required"})
    args = {"request_id": request, "verified_patient_id": 3}
    first = q.handle("waiting.withdraw", args, AT)
    assert first == q.handle("waiting.withdraw", args, AT)
    assert first[0] == "ok"
    result = q.candidates({"slot_id": 1}, AT)[1]
    assert 3 not in {item["patient_id"] for item in result["candidates"]}
    assert crm.reg.execute("SELECT request_at FROM wait_requests WHERE id=?", (request,))


def test_previous_offer_schema_migrates_without_losing_durable_id(queue):
    crm, _ = queue
    first = next_offer(crm.waiting)
    # Recreate the published draft schema, including its lifetime unique key.
    with crm.waiting._transaction() as conn:
        conn.execute("DROP INDEX ix_backfill_one_pending")
        conn.execute("ALTER TABLE backfill_offers RENAME TO modern_offers")
        conn.execute("CREATE TABLE backfill_offers(id INTEGER PRIMARY KEY,slot_id INTEGER NOT NULL,patient_id INTEGER NOT NULL,old_booking_id INTEGER,state TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT,result TEXT,UNIQUE(slot_id,patient_id))")
        conn.execute("INSERT INTO backfill_offers SELECT id,slot_id,patient_id,old_booking_id,state,created_at,expires_at,result FROM modern_offers")
        conn.execute("DROP TABLE modern_offers")
    restarted = ClinicCrm(MockRegistry(crm.reg.path, AT.replace(tzinfo=None)), crm.stand)
    assert next_offer(restarted.waiting) == first


def test_original_request_retry_after_fulfilment_returns_same_event(queue):
    crm, _ = queue
    q = crm.waiting
    crm.reg._x("DELETE FROM wait_requests WHERE patient_id=3")
    args = {
        "request_key": "synthetic-pending-request", "patient_id": 3,
        "doctor_id": 1, "specialty": "терапевт",
        "request_at": "2026-09-30T10:00:00+04:00",
    }
    first = q.handle("waiting.record", args, AT)
    for _ in range(2):
        offer = next_offer(q)
        q.handle("backfill.decline", {"offer_id": offer["offer_id"]}, AT)
    assert accept(q, next_offer(q))[0] == "ok"
    replay = q.handle("waiting.record", args, AT)
    assert replay == ("ok", {"request_id": first[1]["request_id"], "recorded": False})
    assert crm.reg.execute("SELECT state FROM wait_requests WHERE patient_id=3")[0]["state"] == "scheduled"


def test_withdrawn_requests_are_reported_separately_from_ongoing_censoring():
    stats = waiting_estimate([
        {"state": "withdrawn", "request_at": "2026-10-01T10:00:00+04:00"},
        {"state": "pending", "request_at": "2026-10-01T10:00:00+04:00"},
    ], AT, min_samples=1)
    assert stats["samples"] == 1 and stats["withdrawn"] == 1
    assert stats["status"] == "censored"

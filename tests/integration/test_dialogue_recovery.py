"""Runtime recovery against a temporary local stand, never a patient MIS or LLM."""
import pytest

from clinic.crm import ClinicCrm
from clinic.recovery import TRANSFER_MESSAGE
from test_crm import book, clinic  # reuse temporary SQLite/local HTTP-contract stand


def test_future_same_specialty_requires_explicit_choice(clinic):
    crm, stand = clinic
    old = book(crm)[1]["booking_id"]
    status, result = crm.handle("booking_conflict", {"patient_id": 3, "specialty": "терапевт", "call_id": "next"})
    assert status == "conflict" and result["booking_id"] == old
    assert book(crm, "next", slot=2)[0] == "conflict"
    assert len(stand.mock.data["visits"]) == 1
    assert crm.reg.booking(old).status == "booked"
    assert not crm.reg._q("SELECT 1 FROM operations WHERE key='next'")


def test_consent_is_scoped_to_call_and_exact_visits(clinic):
    crm, stand = clinic
    book(crm)
    args = {"patient_id": 3, "specialty": "терапевт", "call_id": "allowed", "decision": "allow_new"}
    assert crm.handle("booking_conflict", args)[0] == "clear"
    assert book(crm, "other-call", slot=2)[0] == "conflict"
    assert book(crm, "allowed", slot=2)[0] == "ok"
    assert len(stand.mock.data["visits"]) == 2
    assert book(crm, "allowed-new-key", slot=3)[0] == "conflict"


def test_past_or_other_specialty_does_not_block_new_visit(clinic, monkeypatch):
    crm, _ = clinic
    old = book(crm)[1]["booking_id"]
    assert crm.handle("booking_conflict", {"patient_id": 3, "specialty": "офтальмолог"})[0] == "clear"
    monkeypatch.setenv("CRM_NOW", "2026-10-16T09:00:00+04:00")
    assert crm.handle("booking_conflict", {"patient_id": 3, "specialty": "терапевт"})[0] == "clear"
    assert crm.reg.booking(old).status == "booked"  # status alone cannot mark a future visit


def test_mis_transaction_phrase_is_not_used_for_form_errors(clinic):
    crm, stand = clinic
    assert crm.handle("book", {"patient_id": 3, "slot_id": 999, "operation_key": "bad", "call_id": "bad"})[0] == "error"
    assert crm.handle("recovery.route", {"call_id": "bad"})[0] == "form_error"
    assert not stand.calls
    stand.fail = "denied"
    book(crm, "runtime-failed")
    status, result = crm.handle("recovery.route", {"call_id": "runtime-failed"})
    assert status == "operator" and result["recovery_message"] == TRANSFER_MESSAGE
    crm.handle("book", {"patient_id": 3, "slot_id": 999, "operation_key": "new-bad", "call_id": "runtime-failed"})
    assert crm.handle("recovery.route", {"call_id": "runtime-failed"})[0] == "form_error"


def test_uncertain_write_cannot_be_retried_with_new_key_or_slot(clinic, monkeypatch):
    crm, stand = clinic
    stand.fail = "timeout"
    assert book(crm)[1]["operation_state"] == "uncertain"
    before = len(stand.calls)
    assert book(crm, "new-key", slot=2)[1]["operation_state"] == "uncertain"
    assert len(stand.calls) == before
    monkeypatch.setenv("CRM_NOW", "2026-10-15T23:00:00+04:00")
    status, result = crm.handle("recovery.route", {"call_id": "new-key"})
    assert status == "callback" and "переключу" not in result["recovery_message"]
    assert len(stand.mock.data["visits"]) == 1


@pytest.mark.parametrize("ci_flag", ["CI", "GITHUB_ACTIONS", "ACT"])
def test_actions_never_calls_reasoning_even_if_enabled(clinic, monkeypatch, ci_flag):
    crm, stand = clinic
    stand.fail = "denied"
    book(crm)
    monkeypatch.setenv("CRM_NOW", "2026-10-15T23:00:00+04:00")
    monkeypatch.setenv("CLINIC_RUNTIME_MODE", "production")
    monkeypatch.setenv("NIGHT_REASONING_ENABLED", "1")
    monkeypatch.setenv(ci_flag, "true")
    def forbidden(_):
        pytest.fail("CI must never invoke a reasoning model")
    crm.recovery_reasoner = forbidden
    assert crm.handle("recovery.route", {"call_id": "first", "analyze": True})[0] == "callback"


def test_reasoning_gets_context_once_and_cannot_write_or_speak_free_text(clinic, monkeypatch):
    crm, stand = clinic
    stand.fail = "denied"
    book(crm)
    monkeypatch.setenv("CRM_NOW", "2026-10-15T23:00:00+04:00")
    monkeypatch.setenv("CLINIC_RUNTIME_MODE", "production")
    monkeypatch.setenv("NIGHT_REASONING_ENABLED", "1")
    for key in ("CI", "GITHUB_ACTIONS", "ACT"):
        monkeypatch.delenv(key, raising=False)
    contexts = []
    def analyze(context):
        contexts.append(context)
        return {"action": "request_information", "text": "Запись создана", "missing_field": "middle_name"}
    crm.recovery_reasoner = analyze
    args = {"call_id": "first", "analyze": True, "context": {"history": ["inbound.book_do"]}}
    status, result = crm.handle("recovery.route", args)
    assert status == "clarify" and result["recovery_question"] == "Уточните, пожалуйста, ваше отчество."
    assert "Запись создана" not in str(result)
    assert contexts[0]["dialogue"] == args["context"]
    assert contexts[0]["writes_forbidden"] and contexts[0]["retry_forbidden"]
    assert crm.handle("recovery.route", args)[0] == "callback"
    assert len(contexts) == 1 and not stand.mock.data["visits"]


@pytest.mark.parametrize("clock,allowed", [("08:00", False), ("09:59", False), ("10:00", True), ("15:59", True), ("16:00", True), ("16:01", False)])
def test_callback_window_excludes_morning_backlog(clinic, clock, allowed):
    crm, _ = clinic
    args = {"call_id": clock, "preferred_date": "2026-10-16", "preferred_time": clock}
    assert (crm.handle("callback_request", args)[0] == "ok") == allowed


def test_no_callback_request_is_persistent_and_never_enqueues_sms(clinic):
    crm, stand = clinic
    args = {"call_id": "request", "patient_id": 3, "phone": "70000000000", "context": {"vars": {"specialty": "терапевт"}}}
    assert crm.handle("booking_request", args)[0] == "invalid"
    status, result = crm.handle("booking_request", {**args, "consent": True})
    assert status == "ok" and not result["booking_confirmed"] and not result["sms_enqueued"]
    assert not crm.reg._q("SELECT * FROM callbacks")
    assert not crm.reg._q("SELECT * FROM sms_outbox") and not stand.calls
    restarted = ClinicCrm(crm.reg, stand)
    assert restarted.reg._q("SELECT contact_policy FROM booking_requests")[0][0] == "sms_after_confirmed_booking"
    assert crm.handle("sms_enqueue", {"call_id": "request", "template": "booked", "vars": {"patient_id": 3}})[0] == "error"


def test_doctor_bank_worker_is_nonblocking_and_keeps_only_latest_pending_snapshot():
    import threading
    import time
    from clinic.doctor_bank_sync import DoctorBankSync
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    snapshots = []
    def rpc(module, op, args):
        assert (module, op) == ("tts", "bank.doctors.refresh")
        snapshots.append(args["doctors"])
        if len(snapshots) == 1:
            started.set()
            assert release.wait(2)
        else:
            finished.set()
    worker = DoctorBankSync(rpc)
    def doctors(i):
        return [{"id": i, "full_name": "Врач Тестовый", "is_active": True}]
    worker.enqueue(doctors(1))
    assert started.wait(1)
    before = time.monotonic()
    worker.enqueue(doctors(2))
    worker.enqueue(doctors(3))
    assert time.monotonic() - before < .2
    release.set()
    assert finished.wait(1)
    assert [s[0]["id"] for s in snapshots] == [1, 3]


def test_reasoning_timeout_falls_back_without_repeating_mis(clinic, monkeypatch):
    import time
    crm, stand = clinic
    stand.fail = "denied"
    book(crm)
    monkeypatch.setenv("CRM_NOW", "2026-10-15T23:00:00+04:00")
    monkeypatch.setenv("CLINIC_RUNTIME_MODE", "production")
    monkeypatch.setenv("NIGHT_REASONING_ENABLED", "1")
    for key in ("CI", "GITHUB_ACTIONS", "ACT"):
        monkeypatch.delenv(key, raising=False)
    def slow(_):
        time.sleep(2.2)
        return {"action": "request_information", "missing_field": "middle_name"}
    crm.recovery_reasoner = slow
    before, requests = time.monotonic(), len(stand.calls)
    assert crm.handle("recovery.route", {"call_id": "first", "analyze": True})[0] == "callback"
    assert time.monotonic() - before < 2.8
    assert len(stand.calls) == requests


def test_pull_enqueues_doctor_bank_without_rpc_in_caller(clinic, monkeypatch):
    import threading
    crm, _ = clinic
    observed = threading.Event()
    def rpc(module, op, args):
        assert threading.current_thread().name == "doctor-bank-sync"
        assert module == "tts" and op == "bank.doctors.refresh"
        assert all({"id", "full_name", "is_active"} == set(d) for d in args["doctors"])
        observed.set()
        return {"doctors": len(args["doctors"])}
    monkeypatch.setenv("TTS_DOCTOR_BANK_SYNC", "1")
    monkeypatch.setattr("clinic.admin_rpc.module_call", rpc)
    status, result = crm.handle("sync_pull", {})
    assert status == "ok" and result["doctor_bank_sync"] == "pending"
    assert observed.wait(1)


def test_invalid_callback_is_a_form_error_not_unscheduled_success(clinic):
    crm, _ = clinic
    assert crm.handle("callback_request", {"call_id": "bad", "preferred_date": "bad", "preferred_time": "99:00"})[0] == "invalid"
    assert not crm.reg._q("SELECT * FROM callbacks")


def test_twenty_concurrent_mutations_fail_fast_when_writer_is_busy(clinic):
    from concurrent.futures import ThreadPoolExecutor
    import time
    crm, stand = clinic
    crm.mutations.acquire()
    try:
        before = time.monotonic()
        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(lambda i: book(crm, f"load-{i}"), range(20)))
        elapsed = time.monotonic() - before
    finally:
        crm.mutations.release()
    assert elapsed < 3.5
    assert all(status == "busy" for status, _ in outcomes)
    assert not stand.calls and not crm.reg._q("SELECT * FROM operations")
    assert not crm.reg._q("SELECT * FROM transaction_failures")


def test_token_budget_rejection_is_busy_without_transaction_failure(clinic, monkeypatch):
    from app.types import MisError
    crm, stand = clinic
    def unavailable(*_args, **_kwargs):
        raise MisError("local budget", 429, "MIS_LOCAL_BUSY")
    monkeypatch.setattr(stand, "request", unavailable)
    assert book(crm)[0] == "busy"
    assert not crm.reg._q("SELECT * FROM transaction_failures")
    assert crm.reg._q("SELECT status FROM slots WHERE id=1")[0][0] == "free"

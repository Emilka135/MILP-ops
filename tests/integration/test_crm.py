from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pytest
from calls.crm.registry import MockRegistry, seed_demo
from clinic.crm import ClinicCrm
from clinic.inputs import load
from clinic.modules import Nlu

from app.mis.mock_stand import MockStand
from app.types import MisError, MisTimeout


class Stand:
    def __init__(self, path, reg):
        self.mock = MockStand(path, seed=False)
        self.mock.data["doctors"] = [
            {
                "id": d["id"],
                "full_name": d["full_name"],
                "specialty": d["specialty"],
                "is_active": True,
            }
            for d in reg.doctors()
        ]
        self.calls = []
        self.fail = None

    def request(self, method, path, body=None, query=None):
        self.calls.append((method, path, body))
        if self.fail == "denied" and path.startswith("/visits"):
            raise MisError("denied", 401, "НЕ_АВТОРИЗОВАН")
        result = self.mock.handle(method, path, query, body)[1]
        if self.fail == "timeout" and path.startswith("/visits"):
            raise MisTimeout("response lost", 0, "ТАЙМАУТ")
        return result

    def pages(self, path):
        return self.request("GET", path)


@pytest.fixture
def clinic(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_NOW", "2026-10-15T11:00:00+04:00")
    reg = MockRegistry(str(tmp_path / "clinic.sqlite"), datetime(2026, 10, 15, 11))
    seed_demo(reg, date(2026, 10, 16))
    stand = Stand(tmp_path / "stand.json", reg)
    crm=ClinicCrm(reg, stand)
    # This fixture explicitly represents a freshly reconciled occupancy snapshot.
    import time
    crm._last_slots_sync=time.monotonic()
    return crm, stand


def book(c, key="first", slot=1, patient=3):
    return c.handle(
        "book",
        {"operation_key": key, "patient_id": patient, "slot_id": slot, "call_id": key},
    )


def test_api_capitalization_does_not_hide_imported_slots(clinic):
    c, stand = clinic
    stand.mock.data['doctors'][0]['specialty'] = 'Терапевт'
    status, _ = c.pull()
    assert status == 'ok'
    assert c.reg.doctors()[0]['specialty'] == 'терапевт'
    assert c.reg.find_slots(specialty='терапевт')


def test_actual_stand_contract_and_local_confirmation(clinic):
    c, stand = clinic
    status, result = book(c)
    assert status == "ok" and result["stand_acknowledged"]
    assert [x[:2] for x in stand.calls] == [("POST", "/clients"), ("POST", "/visits")]
    assert stand.calls[-1][2] == {
        "client_id": 1,
        "doctor_id": 1,
        "scheduled_at": "16.10.2026 08:00",
    }
    bid = result["booking_id"]
    c.handle("confirm", {"operation_key": "confirm", "booking_id": bid})
    assert len(stand.calls) == 2
    status, moved = c.handle(
        "reschedule", {"operation_key": "move", "booking_id": bid, "slot_id": 2}
    )
    assert status == "ok" and moved["booking_id"] == bid
    assert stand.calls[-1][:2] == ("PATCH", "/visits/1")
    assert c.reg.remote_id_of(bid) == "1"
    c.handle("cancel", {"operation_key": "cancel", "booking_id": bid})
    assert stand.calls[-1] == ("PATCH", "/visits/1", {"status": "Отменена"})
    assert len(stand.mock.data["visits"]) == 1
    assert stand.mock.data["visits"][0]["status"] == "Отменена"


def test_no_duplicate_after_retry_and_restart(clinic):
    c, stand = clinic
    first = book(c)
    assert book(c) == first
    restarted = ClinicCrm(MockRegistry(c.reg.path, datetime(2026, 10, 15)), stand)
    assert book(restarted) == first
    assert len(stand.mock.data["visits"]) == 1
    assert book(c, slot=2)[0] == "error"


def test_sync_manual_move_and_cancellation_preserves_local_id(clinic):
    c, stand = clinic
    bid = book(c)[1]["booking_id"]
    stand.request(
        "PATCH", "/visits/1", {"scheduled_at": "16.10.2026 08:15", "doctor_id": 1}
    )
    assert c.handle("sync_pull", {})[0] == "ok"
    assert c.reg.booking(bid).slot_id == 2
    assert c.reg.execute("SELECT status FROM slots WHERE id=1")[0]["status"] == "free"
    assert c.reg.execute("SELECT status FROM slots WHERE id=2")[0]["status"] == "booked"
    stand.request("PATCH", "/visits/1", {"status": "Отменена"})
    assert c.handle("sync_pull", {})[0] == "ok"
    assert c.reg.booking(bid).status == "cancelled"
    assert c.reg.execute("SELECT status FROM slots WHERE id=2")[0]["status"] == "free"


def test_sync_unknown_patient_still_blocks_slot(clinic):
    c, stand = clinic
    client = stand.request(
        "POST",
        "/clients",
        {"last_name": "Другой", "first_name": "Пациент", "birth_date": "01.01.1980"},
    )
    stand.request(
        "POST",
        "/visits",
        {"client_id": client["id"], "doctor_id": 1, "scheduled_at": "16.10.2026 08:00"},
    )
    assert c.handle("sync_pull", {})[0] == "ok"
    assert (
        c.reg.execute("SELECT status FROM slots WHERE id=1")[0]["status"] == "external"
    )
    assert book(c)[0] == "taken"


def test_same_slot_race_has_exactly_one_winner(clinic):
    c, stand = clinic
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda i: book(c, f"race-{i}"), range(16)))
    assert [x[0] for x in results].count("ok") == 1
    assert len(stand.mock.data["visits"]) == 1


def test_uncertain_commit_quarantines_slot_and_never_retries_post(clinic):
    c, stand = clinic
    stand.fail = "timeout"
    status, result = book(c)
    assert status == "error" and result["operation_state"] == "uncertain"
    assert len(stand.mock.data["visits"]) == 1
    assert (
        c.reg.execute("SELECT status FROM slots WHERE id=1")[0]["status"] == "reserved"
    )
    assert c.reg.execute("SELECT * FROM bookings") == []
    assert book(c)[0] == "error"
    assert len(stand.mock.data["visits"]) == 1


def test_definite_rejection_releases_reservation(clinic):
    c, stand = clinic
    stand.fail = "denied"
    assert book(c)[1]["operation_state"] == "failed"
    assert c.reg.execute("SELECT status FROM slots WHERE id=1")[0]["status"] == "free"
    assert not stand.mock.data["visits"]


def test_cancelled_booking_cannot_change(clinic):
    c, _stand = clinic
    bid = book(c)[1]["booking_id"]
    c.handle("cancel", {"operation_key": "cancel", "booking_id": bid})
    assert (
        c.handle(
            "reschedule", {"operation_key": "move", "booking_id": bid, "slot_id": 2}
        )[0]
        == "error"
    )
    assert (
        c.handle("confirm", {"operation_key": "confirm", "booking_id": bid})[0]
        == "error"
    )


def test_inactive_doctor_hidden_and_cannot_book(clinic):
    c, _ = clinic
    c.reg._x("UPDATE doctors SET active=0 WHERE id=1")
    assert book(c)[0] == "error"
    assert all(s.doctor_id != 1 for s in c.reg.find_slots("терапевт"))


def test_sms_persists_and_contains_booked_person(clinic):
    c, _ = clinic
    book(c)
    v = {
        **c.module.op_my_booking(3)[1],
        "patient_id": 3,
        "patient_name": "Мария Сергеевна",
    }
    status, message = c.handle(
        "sms_enqueue", {"template": "booked", "vars": v, "call_id": "test"}
    )
    assert status == "ok" and "Мария Сергеевна" in message["text"]
    restarted = ClinicCrm(MockRegistry(c.reg.path), None)
    assert len(restarted.handle("sms_pending", {})[1]["messages"]) == 1
    c.handle("sms_enqueue", {"template": "booked", "vars": v, "call_id": "test"})
    assert len(c.handle("sms_pending", {})[1]["messages"]) == 1


def test_operator_survives_restart_and_escapes_text(clinic):
    c, _ = clinic
    c.handle(
        "operator_transfer",
        {
            "call_id": "operator-test",
            "reason": "requested",
            "vars": {
                "last_utterance": "<script>alert(1)</script>",
                "specialty": "терапевт",
            },
        },
    )
    restarted = ClinicCrm(MockRegistry(c.reg.path), None)
    page = restarted.handle("operator_list", {})[1]["html"]
    assert "&lt;script&gt;" in page and "<script>" not in page
    assert "background:#c8f7c5" in page


def test_schedule_import_preserves_stand_doctor_id(tmp_path):
    reg = MockRegistry()
    ClinicCrm(reg)
    path = tmp_path / "schedule.csv"
    path.write_text(
        "doctor_id,full_name,specialty,cabinet,date,start,end,slot_minutes\n85,Иванов Иван,терапевт,201,2026-10-16,10:00,11:00,20\n"
    )
    assert load(reg, {"CRM_SCHEDULE": str(path)})["slots"] == 3
    assert reg.doctors()[0]["id"] == 85
    path.write_text(path.read_text().replace(",20\n", ",0\n"))
    with pytest.raises(ValueError):
        load(reg, {"CRM_SCHEDULE": str(path)})


@pytest.mark.parametrize(
    "hour,allowed", [(9, False), (10, True), (21, True), (22, False)]
)
def test_outbound_window_uses_clinic_gmt4(clinic, monkeypatch, hour, allowed):
    c, _ = clinic
    bid = book(c)[1]["booking_id"]
    monkeypatch.setenv("CRM_NOW", f"2026-10-15T{hour:02}:00:00+04:00")
    assert (c.handle("dial_reserve", {"booking_id": bid})[0] == "ok") == allowed


def test_six_attempts_in_three_pairs_and_persistent_phone_budget(clinic, monkeypatch):
    c, _ = clinic
    bid = book(c)[1]["booking_id"]
    for at in ["10:00", "10:20", "13:20", "13:40", "16:40", "17:00"]:
        monkeypatch.setenv("CRM_NOW", f"2026-10-15T{at}:00+04:00")
        status, attempt = c.handle("dial_reserve", {"booking_id": bid})
        assert status == "ok", attempt
        c.handle(
            "dial_result", {"attempt_id": attempt["attempt_id"], "outcome": "no_answer"}
        )
    monkeypatch.setenv("CRM_NOW", "2026-10-15T21:00:00+04:00")
    restarted = ClinicCrm(MockRegistry(c.reg.path), None)
    assert restarted.handle("dial_reserve", {"booking_id": bid})[0] == "blocked"


@pytest.mark.parametrize(
    "phrase,intent",
    [
        ("да, у меня острая боль в груди", "emergency"),
        ("отмените запись, хочу жалобу", "complaint"),
        ("какое лекарство пить", "medical"),
        ("позовите оператора", "operator"),
    ],
)
def test_merged_safety_never_becomes_booking(phrase, intent):
    status, result = Nlu().handle("interpret", {"text": phrase, "today": "2026-10-15"})
    assert status == "ok" and result["intent"] == intent


@pytest.mark.parametrize(
    "phrase,affirmative",
    [
        ("Да, отмените.", True),
        ("Да, нет, не отменяйте.", False),
        ("Не отменяйте.", False),
        ("Отмените запись.", None),
    ],
)
def test_cancel_confirmation_has_separate_context_flag(phrase, affirmative):
    _, result = Nlu().handle("interpret", {"text": phrase})
    assert result["affirmative_action"] is affirmative
    assert result["yes_no"] is not True


def test_real_asr_confusion_does_not_create_booking():
    _, result = Nlu().handle("interpret", {"text": "Принесите запись."})
    assert result["intent"] == "operator"


@pytest.mark.parametrize(
    "phrase",
    ["Не подтверждаю приём.", "Не отменяйте запись.", "Не надо переносить запись."],
)
def test_negated_action_cannot_be_executed_as_positive(phrase):
    _, result = Nlu().handle("interpret", {"text": phrase})
    assert result["intent"] == "operator"


def test_callback_outside_operator_hours_is_persistent(clinic, monkeypatch):
    c, stand = clinic
    monkeypatch.setenv("CRM_NOW", "2026-10-15T23:00:00+04:00")
    assert c.handle("meta", {})[1]["operator_available"] is False
    args = {
        "call_id": "callback-test",
        "phone": "79165550000",
        "reason": "requested",
        "preferred_date": "2026-10-16",
        "preferred_time": "15:00",
    }
    assert (
        c.handle("callback_request", {**args, "preferred_time": "03:00"})[0]
        == "invalid"
    )
    assert c.handle("callback_request", args)[0] == "ok"
    restarted = ClinicCrm(MockRegistry(c.reg.path), stand)
    rows = restarted.handle("callback_list", {})[1]["requests"]
    assert len(rows) == 1 and rows[0]["preferred_at"] == "2026-10-16T15:00:00+04:00"


def test_answered_is_not_verified_contact(clinic, monkeypatch):
    c, _ = clinic
    bid = book(c)[1]["booking_id"]
    attempt = c.handle("dial_reserve", {"booking_id": bid})[1]
    c.handle(
        "dial_result", {"attempt_id": attempt["attempt_id"], "outcome": "answered"}
    )
    monkeypatch.setenv("CRM_NOW", "2026-10-15T11:20:00+04:00")
    assert c.handle("dial_reserve", {"booking_id": bid})[0] == "ok"
    assert (
        c.handle(
            "verify_card", {"patient_id": 3, "card_no": "1003", "booking_id": bid}
        )[0]
        == "ok"
    )
    monkeypatch.setenv("CRM_NOW", "2026-10-15T15:00:00+04:00")
    assert c.handle("dial_reserve", {"booking_id": bid})[0] == "blocked"


@pytest.mark.parametrize(
    "phrase,date,time,part",
    [
        ("четырнадцатого октября в четырнадцать тридцать", "2026-10-14", "14:30", None),
        ("завтра утром", "2026-10-16", None, "morning"),
        ("в пятницу после обеда", "2026-10-16", None, "afternoon"),
    ],
)
def test_canonical_dates_and_times(phrase, date, time, part):
    _, result = Nlu().handle("interpret", {"text": phrase, "today": "2026-10-15"})
    assert result["vars"].get("said_date") == date
    assert result["vars"].get("said_time") == time
    assert result["vars"].get("said_daypart") == part

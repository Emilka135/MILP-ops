"""Input/output contract with the stand: imports (input), fake stand + StandSync + outbox (output)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime

import pytest

from calls.crm.fake_stand import FakeStand
from calls.crm.module import CrmModule
from calls.crm.registry import MockRegistry, seed_demo
from calls.crm.remote import EndpointMap, HttpRegistry, Outbox, RateLimited, TokenBucket, render_template
from calls.crm.stand import StandSync, load_inputs_from_env
from calls.orchestrator.modules import OperatorModule
from tests.conftest import NOW, TODAY, wait_until

TOKEN = "team-token"  # noqa: S105  # the fake stand's demo token


def _reg() -> MockRegistry:
    r = MockRegistry(now=NOW)
    seed_demo(r, TODAY)
    return r


def _get(url: str, token: str | None = TOKEN) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# ---- input --------------------------------------------------------------------------------------------


def test_patients_import_csv_and_xlsx_like_rows(tmp_path):
    reg = _reg()
    csv_path = tmp_path / "patients.csv"
    csv_path.write_text(
        "last_name,first_name,middle_name,birth_date,snils,card_no,phone\n"
        "Иванов,Иван,Иванович,12.05.1980,123-456-789 01,1001,+7 (903) 555-12-34\n"  # existing → updated, not duplicated
        "Орлова,Вера,Павловна,1975-02-28,44455566677,2002,89990000002\n"
        "Без,Данных,,,,,\n",
        encoding="utf-8",
    )
    assert reg.load_patients_csv(csv_path) == 3
    pats = reg.export_patients()
    assert len(pats) == 5  # 3 demo + Орлова + the name-only row (no identifiers → inserted once)
    ivan = reg.find_patient(snils="12345678901")[0]
    assert ivan.id == 1 and ivan.phone == "79035551234"
    orlova = reg.find_patient(card_no="2002")[0]
    assert orlova.birth_date == date(1975, 2, 28) and orlova.phone == "79990000002"
    # Excel-like rows (datetimes, ints) go through the same path
    assert reg.load_patients_rows([{"last_name": "Кот", "first_name": "Пётр", "birth_date": datetime(1999, 1, 2), "card_no": 3003, "phone": 79990000003}]) == 1
    assert reg.find_patient(card_no="3003")[0].birth_date == date(1999, 1, 2)


def test_bookings_import_csv_json_idempotent(tmp_path):
    reg = _reg()
    (tmp_path / "b.csv").write_text("remote_id,patient_id,doctor,starts_at,confirmed\nAPT-7,1,Петров Сергей Иванович,2026-10-15T15:00,0\nAPT-8,3,1,2026-10-15T09:00,1\n", encoding="utf-8")
    assert reg.load_bookings_csv(tmp_path / "b.csv") == 2
    assert reg.load_bookings_csv(tmp_path / "b.csv") == 2  # idempotent by remote_id → still 2 bookings
    assert len(reg.export_bookings()) == 2
    b = reg.booking_by_remote_id("APT-7")
    assert b is not None and b.doctor == "Петров Сергей Иванович" and b.status == "booked" and not b.confirmed
    assert reg.execute("SELECT status FROM slots WHERE id = :id", {"id": b.slot_id})[0]["status"] == "booked"  # taken in the mirror → no double booking
    assert reg.booking_by_remote_id("APT-8").confirmed is True
    (tmp_path / "b.json").write_text(json.dumps({"items": [{"id": "APT-9", "patient_id": 1, "doctor_id": 2, "starts_at": "2026-10-16T14:00", "confirmed": False}]}), encoding="utf-8")
    assert reg.load_bookings_json(tmp_path / "b.json") == 1
    assert reg.sync_report()["bookings"] == {"synced": 3}


def test_load_inputs_from_env(tmp_path):
    reg = MockRegistry(now=NOW)
    (tmp_path / "s.csv").write_text("last_name,full_name,specialty,cabinet,date,start,end,slot_minutes\nИванова,Иванова Мария Петровна,терапевт,101,2026-10-15,08:00,09:00,15\n", encoding="utf-8")
    (tmp_path / "p.csv").write_text("last_name,first_name,middle_name,birth_date,snils,card_no,phone\nИванов,Иван,Иванович,1980-05-12,12345678901,1001,79035551234\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("remote_id,patient_id,doctor,starts_at,confirmed\nAPT-1,1,Иванова Мария Петровна,2026-10-15T08:00,0\n", encoding="utf-8")
    out = load_inputs_from_env(reg, {"CRM_SCHEDULE": str(tmp_path / "s.csv"), "CRM_PATIENTS": str(tmp_path / "p.csv"), "CRM_BOOKINGS": str(tmp_path / "b.csv")}, log=lambda m: None)
    assert out == {"slots": 4, "patients": 1, "bookings": 1}
    assert load_inputs_from_env(reg, {}, log=lambda m: None) == {}


# ---- endpoint map -------------------------------------------------------------------------------------


def test_endpoint_map_json_roundtrip_and_templates(tmp_path):
    em = EndpointMap()
    assert EndpointMap.from_dict(em.to_dict()) == em
    spec = {"book": {"method": "put", "path": "/v2/visits", "body": {"patient": "{patient_id}", "when": "{starts_at}", "note": "call {call_id}", "channel": "ivr"}},
            "cancel": ["POST", "/v2/visits/{remote_id}/cancel"], "operator": {"body": None}, "id_field": "visit_id"}
    path = tmp_path / "stand.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    em2 = EndpointMap.load(str(path))
    assert em2.book == ("PUT", "/v2/visits") and em2.cancel == ("POST", "/v2/visits/{remote_id}/cancel") and em2.id_field == "visit_id"
    assert em2.render_body("book", {"patient_id": 7, "starts_at": "2026-10-15T10:00", "call_id": "c1"}) == {"patient": 7, "when": "2026-10-15T10:00", "note": "call c1", "channel": "ivr"}
    assert em2.render_body("transfer", {"call_id": "c1", "reason": "pain", "x": None}) == {"call_id": "c1", "reason": "pain"}  # no template → payload minus None
    assert em2.render_body("cancel", {"remote_id": "A"}) == {"remote_id": "A"}  # POST without template → the payload itself
    assert render_template({"a": "{missing}", "b": {"c": "{x}"}, "d": 1}, {"x": 2}) == {"b": {"c": 2}, "d": 1}


# ---- fake stand ---------------------------------------------------------------------------------------


def test_fake_stand_auth_crud_and_rate_limit():
    stand = FakeStand(TOKEN, rpm=3)
    srv, url = stand.start_in_thread()
    try:
        assert _get(f"{url}/health", token=None)[0] == 200
        assert _get(f"{url}/api/v1/appointments", token=None)[0] == 401
        assert _get(f"{url}/api/v1/appointments", token="wrong")[0] == 401  # noqa: S106
        st, rec = stand.handle("POST", "/api/v1/appointments", {"patient_id": 1, "doctor_id": 2, "slot_id": 5, "starts_at": "2026-10-15T10:00"}, f"Bearer {TOKEN}")
        assert st == 201 and rec["id"] == "APT-1" and rec["status"] == "booked"
        assert stand.handle("POST", "/api/v1/appointments", {"patient_id": 3, "doctor_id": 2, "starts_at": "2026-10-15T10:00"}, f"Bearer {TOKEN}")[0] == 409  # same doctor+time
        assert stand.handle("POST", "/api/v1/appointments/APT-1/confirm", None, f"Bearer {TOKEN}")[0] == 200
        # 4th request within the minute → 429 (health/admin are exempt)
        st, body = stand.handle("GET", "/api/v1/appointments/APT-1", None, f"Bearer {TOKEN}")
        assert st == 429 and body["retry_after"] > 0 and stand.rejected_429 == 1
        assert _get(f"{url}/admin/log", token=None)[1]["rejected_429"] == 1
        stand.reset()
        stand.rpm = 10
        assert stand.handle("PATCH", "/api/v1/appointments/APT-1", {"starts_at": "x"}, f"Bearer {TOKEN}")[0] == 404
        st, rec = stand.handle("POST", "/api/v1/appointments", {"patient_id": 1, "starts_at": "2026-10-15T10:00"}, f"Bearer {TOKEN}")
        st, rec = stand.handle("PATCH", f"/api/v1/appointments/{rec['id']}", {"starts_at": "2026-10-16T11:00", "slot_id": 9}, f"Bearer {TOKEN}")
        assert st == 200 and rec["starts_at"] == "2026-10-16T11:00" and rec["rescheduled"] and rec["history"][0]["starts_at"] == "2026-10-15T10:00"
        assert stand.handle("DELETE", f"/api/v1/appointments/{rec['id']}", None, f"Bearer {TOKEN}")[1]["status"] == "cancelled"
        assert stand.handle("GET", "/openapi.json", None, None)[1]["openapi"].startswith("3.")
    finally:
        srv.shutdown()
        srv.server_close()


# ---- StandSync: the glue --------------------------------------------------------------------------------


@pytest.fixture()
def live():
    stand = FakeStand(TOKEN, rpm=600)
    srv, url = stand.start_in_thread()
    reg = _reg()
    sync = StandSync(reg, HttpRegistry(url, TOKEN, EndpointMap(), TokenBucket(600, 60))).start()
    crm = CrmModule(reg, today=lambda: TODAY, on_write=sync.on_write)
    operator = OperatorModule(reg, on_ticket=lambda t: sync.on_write("transfer", t))
    yield stand, reg, sync, crm, operator
    sync.stop()
    srv.shutdown()
    srv.server_close()


def test_sync_book_cancel_confirm_reschedule_transfer(live):
    stand, reg, sync, crm, operator = live
    slot = reg.find_slots("терапевт", limit=1)[0]
    st, out = crm.handle("book", {"patient_id": 1, "slot_id": slot.id, "call_id": "c1"})
    assert st == "ok"
    bid = out["booking_id"]
    assert sync.join(5)
    assert reg.remote_id_of(bid) == "APT-1" and reg.sync_report()["bookings"] == {"synced": 1}
    assert stand.appointments["APT-1"]["call_id"] == "c1" and stand.appointments["APT-1"]["source"] == "voice-bot"
    # confirm / reschedule / cancel resolve the stand id from the mirror (FIFO: book is always ahead)
    assert crm.handle("confirm", {"booking_id": bid})[0] == "ok"
    other = [s for s in reg.find_slots("терапевт", limit=5) if s.id != slot.id][0]
    st, out2 = crm.handle("reschedule", {"booking_id": bid, "slot_id": other.id})
    assert st == "ok"
    assert crm.handle("cancel", {"booking_id": out2["booking_id"]})[0] == "ok"
    assert sync.join(5)
    log = [(e["method"], e["path"], e["status"]) for e in stand.log]
    assert log == [("POST", "/api/v1/appointments", 201), ("POST", "/api/v1/appointments/APT-1/confirm", 200),
                   ("PATCH", "/api/v1/appointments/APT-1", 200), ("DELETE", "/api/v1/appointments/APT-1", 200)]
    assert stand.appointments["APT-1"]["status"] == "cancelled" and reg.remote_id_of(out2["booking_id"]) == "APT-1"
    # operator ticket → operator-requests, with the mirror's request id marked synced
    st, t = operator.handle("transfer", {"call_id": "c2", "reason": "human_only", "vars": {"patient_id": 1, "specialty": "кардиолог"}, "history": [], "utterances": ["болит"]})
    assert st == "ok" and sync.join(5)
    req = list(stand.operator_requests.values())[0]
    assert req["reason"] == "human_only" and req["patient_id"] == 1 and "кардиолог" in req["summary"] and req["call_id"] == "c2"
    assert reg.sync_report()["operator_requests"] == {"synced": 1}
    s = sync.status()
    assert s["done"] == 5 and s["failed"] == 0 and s["pending"] == 0 and s["mode"] == "live"


def test_sync_failure_becomes_operator_request_and_never_blocks_the_call():
    reg = _reg()
    calls = []

    def sink(kind, payload):
        calls.append(kind)
        raise RuntimeError("HTTP 500")

    sync = StandSync(reg, None, mode="dry")  # construct, then swap in a failing sink with no sleeps
    sync.outbox = Outbox(sync._sink, on_failure=sync._failed, max_attempts=2, sleep=lambda s: None)  # noqa: SLF001
    sync.client = type("C", (), {"write": staticmethod(sink), "endpoints": EndpointMap(), "base_url": "x", "bucket": TokenBucket()})()
    sync.mode = "live"
    crm = CrmModule(reg, today=lambda: TODAY, on_write=sync.on_write)
    slot = reg.find_slots("терапевт", limit=1)[0]
    st, out = crm.handle("book", {"patient_id": 1, "slot_id": slot.id, "call_id": "c9"})
    assert st == "ok"  # the caller got the answer immediately: the mirror is the source of truth for the dialogue
    while sync.outbox.pending():
        sync.outbox.process_one()
    assert calls == ["book", "book"]
    assert reg.sync_report()["bookings"] == {"failed": 1}
    rows = reg.execute("SELECT call_id, reason, payload FROM operator_requests")
    assert len(rows) == 1 and rows[0]["reason"] == "stand_sync_failed" and rows[0]["call_id"] == "c9" and "HTTP 500" in rows[0]["payload"]
    assert sync.status()["failed"] == 1


def test_rate_limited_waits_instead_of_failing():
    waits = []
    n = {"i": 0}

    def sink(kind, payload):
        n["i"] += 1
        if n["i"] < 3:
            raise RateLimited(7.0)
        return {"id": "A"}

    ob = Outbox(sink, on_failure=lambda it: pytest.fail("must not fail"), max_attempts=1, sleep=waits.append)
    ob.put("book", {"booking_id": 1})
    while ob.pending():
        ob.process_one()
    assert waits == [7.0, 7.0] and ob.items[0].done and ob.items[0].attempts == 0 and ob.rate_waits == 2


def test_http_registry_maps_429_and_5xx():
    class Resp:
        def __init__(self, code, body, headers=None):
            self.code, self.body, self.headers = code, body, headers or {}

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "42"}, None)

    hr = HttpRegistry("http://stand", TOKEN, EndpointMap(), TokenBucket(10, 60), opener=opener)
    with pytest.raises(RateLimited) as ei:
        hr.write("book", {"patient_id": 1})
    assert ei.value.retry_after == 42.0

    def opener5(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "down", {}, None)

    from calls.crm.remote import Unavailable

    with pytest.raises(Unavailable):
        HttpRegistry("http://stand", TOKEN, EndpointMap(), TokenBucket(10, 60), opener=opener5).write("book", {"patient_id": 1})


def test_dry_mode_journals_without_network(tmp_path):
    reg = _reg()
    journal = tmp_path / "stand.jsonl"
    sync = StandSync.from_env(reg, {"STAND_MODE": "dry", "STAND_JOURNAL": str(journal)})
    assert sync is not None and sync.mode == "dry"
    crm = CrmModule(reg, today=lambda: TODAY, on_write=sync.on_write)
    slot = reg.find_slots("педиатр", limit=1)[0]
    bid = crm.handle("book", {"patient_id": 2, "slot_id": slot.id})[1]["booking_id"]
    assert sync.join(5)
    assert reg.remote_id_of(bid).startswith("dry-book-") and reg.sync_report()["bookings"] == {"synced": 1}
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["kind"] == "book"
    sync.stop()
    assert StandSync.from_env(reg, {}) is None
    with pytest.raises(ValueError):
        StandSync.from_env(reg, {"STAND_MODE": "live"})


def test_pull_bookings_from_stand(live):
    stand, reg, sync, crm, operator = live
    stand.appointments["APT-55"] = {"id": "APT-55", "status": "booked", "confirmed": False, "patient_id": 3, "doctor_id": 1, "starts_at": "2026-10-15T08:30", "source": "registrar"}
    assert sync.pull_bookings(date(2026, 10, 15)) == 1
    b = reg.booking_by_remote_id("APT-55")
    assert b is not None and b.patient_id == 3 and b.starts_at == datetime(2026, 10, 15, 8, 30)
    # the reminder flow can now be built from it, and a cancel goes to the right stand id
    st, vars = crm.handle("reminder_vars", {"remote_id": "APT-55"})
    assert st == "ok" and vars["booking_id"] == b.id and vars["patient_name"] == "Мария Сергеевна"
    crm.handle("cancel", {"booking_id": b.id, "call_id": "rem-1"})
    assert wait_until(lambda: stand.appointments["APT-55"]["status"] == "cancelled")


def test_shipped_samples_and_endpoint_config_load():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    reg = MockRegistry(now=NOW)
    out = load_inputs_from_env(reg, {"CRM_SCHEDULE": str(root / "data/samples/schedule.csv"), "CRM_PATIENTS": str(root / "data/samples/patients.csv"),
                                     "CRM_BOOKINGS": str(root / "data/samples/bookings.csv")}, log=lambda m: None)
    assert out["slots"] > 100 and out["patients"] == 3 and out["bookings"] == 2
    assert reg.find_patient(phone="79165550000")[0].id == 3  # "8 916 555-00-00" normalised
    assert reg.booking_by_remote_id("APT-100").doctor == "Петров Сергей Иванович"
    # the shipped endpoint file is the documented default contract (edit it when the organisers' OpenAPI arrives)
    assert EndpointMap.load(str(root / "config/stand.endpoints.json")) == EndpointMap()


def test_services_honour_frozen_clock(monkeypatch):
    from calls.orchestrator.app import Orchestrator
    from pathlib import Path

    from calls.services.__main__ import frozen_now, today

    monkeypatch.setenv("CRM_NOW", "2026-10-14T09:00")
    monkeypatch.delenv("STAND_URL", raising=False)
    assert frozen_now() == datetime(2026, 10, 14, 9, 0) and today() == date(2026, 10, 14)
    reg = MockRegistry(now=frozen_now())
    seed_demo(reg, today())
    monkeypatch.setenv("SCENARIOS_DIR", str(Path(__file__).resolve().parents[1] / "scenarios"))
    orch = Orchestrator.from_env(reg)
    try:
        assert orch.today() == date(2026, 10, 14) and orch.stand is None
    finally:
        orch.bus.stop()
    monkeypatch.delenv("CRM_NOW")
    assert frozen_now() is None and today() == date.today()

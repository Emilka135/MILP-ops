import json
import threading
import urllib.request
from datetime import date

import pytest

from calls.crm.operator import build_ticket, scrub
from calls.crm.remote import EndpointMap, HttpRegistry, Outbox, TokenBucket
from calls.nlu.agent import TwoAgentNLU
from calls.nlu.embed import EmbeddingIntentClassifier, HashEmbedder
from calls.nlu.intent import classify, extract_specialty, extract_surname
from calls.nlu.text2sql import compile_query
from calls.services.http import make_server
from tests.conftest import TODAY

# ---- rules ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,intent", [
    ("хочу записаться к терапевту на завтра", "book"), ("мне бы к кардиологу", "book"), ("к зубному", "book"),
    ("хочу перенести запись на другой день", "reschedule"), ("не смогу прийти, давайте перенесём", "reschedule"),
    ("не смогу прийти, отмените пожалуйста", "cancel"), ("подтверждаю, приду", "confirm"), ("когда у меня запись", "my_booking"),
    ("во сколько работает поликлиника в субботу", "info_schedule"), ("вы работаете в праздники", "info_holidays"),
    ("какой у вас адрес", "info_address"), ("какие документы взять с собой", "info_docs"), ("нужно ли направление к неврологу", "referral"),
    ("хочу пожаловаться на врача", "complaint"), ("у меня острая боль в животе", "emergency"), ("можно ли пить эти таблетки с алкоголем", "medical"),
    ("соедините с оператором", "operator"), ("дайте живого человека", "operator"), ("абракадабра", "unknown"), ("алло", "unknown"),
])
def test_rule_intents(text, intent):
    r = classify(text, TODAY)
    assert r.intent == intent, r.scores
    if intent != "unknown":
        assert r.confidence >= 0.6
    assert r.human_only == (intent in {"complaint", "emergency", "medical", "operator"})


def test_entities():
    r = classify("запишите ребёнка к лору в пятницу после обеда", TODAY)
    e = r.entities
    assert e.specialty == "отоларинголог" and e.for_other is True and not e.needs_referral
    assert e.interpretation.date == date(2026, 10, 16) and e.interpretation.daypart == "afternoon"
    assert classify("мне к кардиологу", TODAY).entities.needs_referral
    assert extract_specialty("флюорография") == "флюорография" and extract_specialty("к глазному") == "офтальмолог"
    assert extract_surname("к доктору Ивановой") == "Ивановой" and extract_surname("к терапевту") is None
    assert extract_surname("хочу к сидоренко", known=["Сидоренко"]) == "Сидоренко"


# ---- embeddings + two agents ---------------------------------------------------------------------------


def test_embedding_classifier_with_hash_embedder():
    clf = EmbeddingIntentClassifier(HashEmbedder()).fit()
    assert clf.predict("хочу записаться к врачу на этой неделе").intent == "book"
    assert clf.predict("отмените пожалуйста мою запись").intent == "cancel"
    assert clf.predict("переключите на человека").intent == "operator"
    assert clf.predict("алло кто это").intent == "unknown"


def test_two_agent_decisions():
    nlu = TwoAgentNLU()
    assert nlu.decide("хочу записаться к терапевту", TODAY, have=["patient"]).action == "execute"
    d = nlu.decide("отмените запись", TODAY)
    assert d.action == "clarify" and set(d.missing) == {"booking", "patient"}
    assert nlu.decide("у меня острая боль", TODAY).action == "operator"
    assert nlu.decide("к доктору Иванову", TODAY, have=["patient"]).action == "operator"  # specific surname → human
    assert nlu.decide("бла бла бла", TODAY).action == "clarify"


# ---- text → SQL automaton --------------------------------------------------------------------------------


def test_text2sql_queries_run_against_the_mirror(reg):
    q = compile_query("запишите меня к кардиологу на завтра после обеда", TODAY)
    assert q.kind == "find_slots" and q.params["specialty"] == "кардиолог" and q.params["day"] == "2026-10-15" and q.params["t_from"] == "13:00"
    rows = reg.execute(q.sql, q.params)
    assert rows and all(r["specialty"] == "кардиолог" and r["starts_at"].startswith("2026-10-15T1") for r in rows)
    q = compile_query("хочу к лору во вторник в 10 утра", TODAY)
    assert q.params["day"] == "2026-10-20" and q.params["t_from"] == "10:00"
    q = compile_query("отмените мою запись", TODAY)
    assert q.kind == "cancel_booking" and set(q.needs) == {"booking_id", "patient_id"} and q.sql.startswith("UPDATE bookings SET status = 'cancelled'")
    assert compile_query("когда у меня запись", TODAY).kind == "my_booking"
    assert compile_query("как работает поликлиника в праздники", TODAY).kind == "holidays"
    assert compile_query("во сколько работает терапевт", TODAY).kind == "doctor_hours"
    assert reg.execute(compile_query("как работает поликлиника в праздники", TODAY).sql, {"day": "2026-10-14"})


@pytest.mark.parametrize("text", ["к доктору Иванову", "у меня острая боль", "просто поболтать", "хочу пожаловаться"])
def test_text2sql_escalates_unsafe_requests(text):
    q = compile_query(text, TODAY)
    assert q.escalate and q.sql == ""


def test_text2sql_cancel_then_reschedule_in_one_sentence_prefers_reschedule():
    assert compile_query("отмените и перенесите на пятницу", TODAY).kind == "reschedule_candidates"


# ---- operator ticket ------------------------------------------------------------------------------------


def test_operator_ticket_highlights_matching_slots(reg):
    vars = {"specialty": "терапевт", "said_date": "2026-10-16", "said_daypart": "afternoon", "patient_id": 1, "patient_name": "Иван Иванович",
            "last_intent": "book", "snils": "12345678901", "doctor_surname": "Петрову"}
    t = build_ticket(reg, "call-1", "specific_doctor", vars, ["inbound.welcome", "inbound.operator"], ["к доктору петрову в пятницу после обеда"])
    assert t.highlighted_slot_ids and all(reg.slot(i).starts_at.date().isoformat() == "2026-10-16" and reg.slot(i).clock.hour >= 13 for i in t.highlighted_slot_ids)
    assert "date(s.starts_at) = :day" in t.highlight_sql and t.highlight_params["day"] == "2026-10-16"
    html = t.to_html()
    assert html.count("background:#c8f7c5") == len({s["id"] for s in t.slots} & set(t.highlighted_slot_ids))
    assert "Петрову" in t.notes[0] and "12345678901" not in t.to_json()
    assert scrub(vars)["snils"] == "***" and scrub(vars)["specialty"] == "терапевт"
    assert reg.execute("SELECT count(*) AS n FROM operator_requests")[0]["n"] == 1


# ---- remote CRM: rate limit + outbox ----------------------------------------------------------------------


def test_token_bucket_10_per_minute():
    now = [0.0]
    sleeps = []
    tb = TokenBucket(10, 60.0, clock=lambda: now[0], sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)))
    for _ in range(10):
        assert tb.acquire()
    assert tb.wait_time() > 59
    assert tb.acquire(timeout=0.1) is False
    assert tb.acquire(timeout=120) is True and now[0] >= 60


def test_outbox_retries_then_reports_failure():
    calls = []
    failed = []

    def sink(kind, payload):
        calls.append(kind)
        if payload.get("bad") and len([c for c in calls if c == kind]) < 3:
            raise RuntimeError("503")
        return {"id": "remote-1"}

    ob = Outbox(sink, on_failure=failed.append, max_attempts=3, sleep=lambda s: None)
    ob.put("book", {"booking_id": 1})
    ob.put("cancel", {"booking_id": 2, "bad": True})
    while ob.pending():
        ob.process_one()
    assert ob.items[0].done and ob.items[0].remote == {"id": "remote-1"}
    assert ob.items[1].done and ob.items[1].attempts == 2 and failed == []
    ob2 = Outbox(lambda k, p: (_ for _ in ()).throw(RuntimeError("down")), on_failure=failed.append, max_attempts=2, sleep=lambda s: None)
    ob2.put("book", {"booking_id": 3})
    while ob2.pending():
        ob2.process_one()
    assert failed and failed[0].attempts == 2 and "down" in failed[0].last_error


def test_http_registry_builds_requests():
    seen = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url, req.data, req.get_header("Authorization")))
        return Resp(b'{"id": "A1"}')

    hr = HttpRegistry("https://stand.example/", "tok", EndpointMap(), TokenBucket(10, 60), opener=opener)
    assert hr.write("book", {"patient_id": 1, "slot_id": 5}) == {"id": "A1"}
    assert hr.write("cancel", {"remote_id": "A1"}) == {"id": "A1"}
    # the default body template adds the provenance fields and drops unresolved placeholders (doctor_id/starts_at/call_id)
    assert seen[0][:2] == ("POST", "https://stand.example/api/v1/appointments") and json.loads(seen[0][2]) == {"patient_id": 1, "slot_id": 5, "source": "voice-bot"}
    assert seen[1][:3] == ("DELETE", "https://stand.example/api/v1/appointments/A1", None) and seen[1][3] == "Bearer tok"


# ---- HTTP service wrapper --------------------------------------------------------------------------------


def test_module_http_service_roundtrip():
    def handler(op, args):
        if op == "echo":
            return "ok", {"got": args, "blob": b"\x00\x01"}
        return "error", {"error": "nope"}

    srv = make_server("demo", handler, "127.0.0.1", 0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            assert json.loads(r.read())["module"] == "demo"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/echo", data=json.dumps({"a": 1}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            body = json.loads(r.read())
        assert body["status"] == "ok" and body["payload"]["got"] == {"a": 1} and body["payload"]["blob_b64"] == "AAE="
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as r:
            assert 'module_requests_total{module="demo"} 1' in r.read().decode()
    finally:
        srv.shutdown()

"""Feedback remains durable, phone-scoped, and cannot deploy unverified scenarios."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest

from calls.crm.registry import MockRegistry
from clinic.crm import ClinicCrm
from clinic.insights import Insights, clusters
from clinic.learning import Learning

AT = datetime(2026, 10, 19, 21, tzinfo=timezone(timedelta(hours=4)))
ADMIN = {"role": "admin"}
REGISTRAR = {"role": "registrar", "registrar": "100"}


@pytest.fixture
def insights(tmp_path):
    return Insights(MockRegistry(str(tmp_path / "insights.sqlite"), AT))


def submit(service, text="Говорите медленнее", *, phone="100", call="call-1", **extra):
    return service.handle("feedback.submit", {"call_id": call, "registrar": phone, "text": text, **extra}, AT)


def dashboard(service, actor=ADMIN, **args):
    status, result = service.handle("insights.dashboard", args, AT, actor=actor)
    assert status == "ok"
    return result


def proposal(service, phone="100"):
    status, queued = service.handle("profile.queue", {"registrar": phone}, AT)
    assert status == "ok"
    assert service.handle("nightly.run", {}, AT, closed=True)[0] == "ok"
    return json.loads(service.reg.execute("SELECT result FROM night_jobs WHERE id=?", (queued["job_id"],))[0]["result"])["profile_id"]


def test_durable_free_text_idempotency_and_no_invented_stars(insights):
    first = submit(insights, "Отлично, всё хорошо, но говорите медленнее")
    second = submit(insights, "Отлично, всё хорошо, но говорите медленнее")
    assert first == second
    restored = Insights(MockRegistry(insights.reg.path, AT))
    result = dashboard(restored)
    assert len(result["feedback"]) == 1
    assert result["feedback"][0]["rating"] is None
    assert result["feedback"][0]["text"] == "Отлично, всё хорошо, но говорите медленнее"
    assert len(result["jobs"]) == 1
    assert result["metrics"][0]["explicit_rating_count"] == 0
    assert result["metrics"][0]["explicit_rating_mean"] is None


def test_rating_and_complaint_metrics_are_distinct(insights):
    submit(insights, "Отлично", call="positive", rating=5)
    submit(insights, "Я недоволен", call="negative")
    submit(insights, "Хочу другую скорость", call="neutral")
    result = dashboard(insights)
    metric = result["metrics"][0]
    assert metric["feedback_count"] == 3
    assert metric["complaint_count"] == 1
    assert metric["complaint_share"] == pytest.approx(1 / 3)
    assert metric["explicit_rating_count"] == 1
    assert metric["explicit_rating_mean"] == 5
    assert result["feedback"][-1]["rating_source"] == "patient_explicit"


@pytest.mark.parametrize("rating", [0, 6, "5", True, 2.5])
def test_invalid_ratings_do_not_create_records(insights, rating):
    assert submit(insights, rating=rating)[0] == "error"
    assert dashboard(insights)["feedback"] == []


def test_old_heuristic_rating_is_not_patient_vote(insights):
    insights.reg._x("INSERT INTO feedback(call_id,registrar,text,kind,rating,created_at) VALUES(?,?,?,?,?,?)", ("old", "100", "Отлично", "suggestion", 5, AT.isoformat()))
    metric = dashboard(insights)["metrics"][0]
    assert metric["explicit_rating_count"] == 0
    assert metric["explicit_rating_mean"] is None


def test_complaint_atomic_suspension_only_its_phone_and_learning_cases(insights):
    learning = Learning(insights.reg)
    for phone in ("100", "200"):
        pid = proposal(insights, phone)
        insights.reg._x("UPDATE registrar_profiles SET state='active' WHERE id=?", (pid,))
        insights.reg._x("INSERT INTO learning_cases(registrar,call_id,source_state,title,intent,examples,response,status,created_at,effective_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (phone, "old", "inbound.welcome", "Раньше ответить", "book", "[]", "Ответ", "active", AT.isoformat(), AT.isoformat(), (AT + timedelta(days=1)).isoformat()))
    status, result = submit(insights, "Система не работает")
    assert status == "ok" and result["kind"] == "complaint"
    profiles = {row["registrar"]: row["state"] for row in dashboard(insights)["profiles"]}
    assert profiles == {"100": "suspended", "200": "active"}
    cases = {row["registrar"]: row["status"] for row in insights.reg.execute("SELECT registrar,status FROM learning_cases")}
    assert cases == {"100": "suspended", "200": "active"}
    assert len(dashboard(insights)["complaints"]) == 1
    assert learning.handle("learning.alias", {"registrar": "100", "state": "inbound.welcome", "text": "ответ"}, AT)[0] == "empty"


def test_failed_suspension_rolls_back_feedback_and_complaint(insights, monkeypatch):
    def unavailable(phone):
        raise RuntimeError("injected write failure")
    monkeypatch.setattr(insights, "_suspend", unavailable)
    with pytest.raises(RuntimeError, match="injected"):
        submit(insights, "Не работает")
    result = dashboard(insights)
    assert result["feedback"] == result["complaints"] == result["jobs"] == []


def test_existing_feedback_can_be_escalated_idempotently_to_complaint(insights):
    first = submit(insights, "Предпочитаю другой ответ")
    escalated = submit(insights, "Предпочитаю другой ответ", kind="complaint")
    assert first[1]["feedback_id"] == escalated[1]["feedback_id"]
    assert escalated[1]["kind"] == "complaint"
    assert len(dashboard(insights)["complaints"]) == 1


def test_feedback_call_cannot_move_to_another_phone(insights):
    assert submit(insights)[0] == "ok"
    assert submit(insights, phone="200")[0] == "forbidden"
    assert submit(insights, "Другой текст", phone="200")[0] == "forbidden"
    assert len(dashboard(insights)["feedback"]) == 1


def test_archive_phone_is_checked_before_first_feedback(insights):
    learning = Learning(insights.reg)
    assert learning.handle("learning.archive", {"call_id": "archived", "registrar": "200"}, AT)[0] == "ok"
    assert submit(insights, call="archived")[0] == "forbidden"
    assert submit(insights, call="archived", phone="200")[0] == "ok"


def test_scoped_dashboard_and_proposal_ignore_browser_phone(insights):
    submit(insights, phone="100", call="one")
    submit(insights, "Меню не работает", phone="200", call="two")
    proposal(insights, "200")
    result = dashboard(insights, REGISTRAR, registrar="200")
    assert {row["registrar"] for row in result["feedback"]} == {"100"}
    assert {row["registrar"] for row in result["clusters"]} == {"100"}
    assert result["complaints"] == result["profiles"] == result["jobs"] == []
    assert {row["registrar"] for row in result["metrics"]} == {"100"}
    status, queued = insights.handle("profile.queue", {"registrar": "200"}, AT, actor=REGISTRAR)
    assert status == "ok"
    payload = json.loads(insights.reg.execute("SELECT payload FROM night_jobs WHERE id=?", (queued["job_id"],))[0]["payload"])
    assert payload["registrar"] == "100"


@pytest.mark.parametrize("actor", [None, {"role": "observer"}, REGISTRAR])
def test_only_trusted_admin_context_can_review_or_resolve(insights, actor):
    for operation in ("profile.review", "complaint.resolve"):
        assert insights.handle(operation, {"id": 1, "approve": True, "role": "admin", "actor": ADMIN, "resolution": "исправлено"}, AT, actor=actor)[0] == "forbidden"


def test_anonymous_dashboard_and_all_observer_operations_are_denied(insights):
    assert insights.handle("insights.dashboard", {"role": "admin"}, AT)[0] == "forbidden"
    for op in ("insights.dashboard", "profile.queue", "feedback.submit", "nightly.run"):
        assert insights.handle(op, {}, AT, closed=True, actor={"role": "observer"})[0] == "forbidden"


def test_complaints_block_approval_resolution_never_reactivates(insights):
    pid = proposal(insights)
    approved = insights.handle("profile.review", {"id": pid, "approve": True}, AT, actor=ADMIN)
    assert approved[0] == "ok"
    submit(insights, "Ужас, неверный ответ")
    assert insights.handle("profile.review", {"id": pid, "approve": True}, AT, actor=ADMIN) == ("blocked", {"error": "unresolved_complaints"})
    complaint = dashboard(insights)["complaints"][0]
    assert insights.handle("complaint.resolve", {"id": complaint["id"], "resolution": ""}, AT, actor=ADMIN)[0] == "error"
    assert insights.handle("complaint.resolve", {"id": complaint["id"], "resolution": "Ответ перепроверен администратором"}, AT, actor=ADMIN)[1]["profile_reactivation"] is False
    assert dashboard(insights)["profiles"][0]["state"] == "suspended"
    assert insights.handle("profile.review", {"id": pid, "approve": True}, AT, actor=ADMIN)[0] == "ok"
    assert insights.handle("profile.route", {"registrar": "100"}, AT)[0] == "empty"


def test_arbitrary_flow_name_or_legacy_active_flag_cannot_enable_route(insights):
    pid = proposal(insights)
    result = insights.handle("profile.review", {"id": pid, "approve": True, "flow_name": "inbound", "verified": True}, AT, actor=ADMIN)
    assert result[0] == "ok" and result[1]["deployment"] == "blocked"
    assert dashboard(insights)["profiles"][0]["flow_name"] is None
    insights.reg._x("UPDATE registrar_profiles SET state='active',flow_name='inbound' WHERE id=?", (pid,))
    assert insights.handle("profile.route", {"registrar": "100", "verified": True}, AT) == ("empty", {"reason": "deployment_verifier_required"})


def test_shared_relational_hours_and_calendar_control_nightly(tmp_path, monkeypatch):
    crm = ClinicCrm(MockRegistry(str(tmp_path / "crm.sqlite"), AT))
    submit(crm.insights)
    monkeypatch.setenv("CRM_NOW", AT.replace(hour=10).isoformat())
    assert crm.handle("nightly.run", {}) == ("blocked", {"reason": "clinic_is_open", "jobs_run": 0})
    assert dashboard(crm.insights)["clusters"] == []
    crm.reg._x("INSERT INTO service_calendar VALUES(?,?,?,?,?,?)", ("registrar", AT.date().isoformat(), None, None, 1, "synthetic closure"))
    assert crm.handle("nightly.run", {})[1]["jobs_run"] == 1
    assert len(dashboard(crm.insights)["clusters"]) == 1


def test_nightly_incremental_clusters_keep_history_and_phone_isolation(insights):
    submit(insights, "Говорите медленнее", call="one")
    submit(insights, "Говорите медленнее пожалуйста", call="two")
    submit(insights, "Говорите медленнее", phone="200", call="three")
    assert insights.handle("nightly.run", {}, AT, closed=False)[0] == "blocked"
    assert insights.handle("nightly.run", {}, AT, closed=True)[1]["jobs_run"] == 1
    first = dashboard(insights)
    assert len(first["clusters"]) == 2
    assert sorted(row["size"] for row in first["clusters"]) == [1, 2]
    original = {row["id"]: row["cluster_id"] for row in first["feedback"]}
    assert insights.handle("nightly.run", {}, AT, closed=True)[1]["jobs_run"] == 0
    submit(insights, "Нужна другая кнопка", call="four")
    insights.handle("nightly.run", {}, AT, closed=True)
    updated = dashboard(insights)
    assert len(updated["clusters"]) == 3
    assert all(original[row["id"]] == row["cluster_id"] for row in updated["feedback"] if row["id"] in original)
    assert all(row["cluster_id"] is not None for row in updated["feedback"])


def test_profile_summary_uses_only_own_feedback_and_never_trains_or_activates(insights):
    submit(insights, "Фамилию распознаёт неверно", phone="200", call="foreign")
    submit(insights, "Говорите медленнее", call="mine")
    pid = proposal(insights)
    profile = next(row for row in dashboard(insights)["profiles"] if row["id"] == pid)
    evidence = json.loads(profile["proposal"])
    assert evidence["themes"] == {"темп": 1}
    assert evidence["evidence_count"] == 1
    assert evidence["automatic_activation"] is False
    assert evidence["neural_training"] is False
    assert profile["state"] == "proposed"
    assert profile["flow_name"] is None


def test_bad_job_does_not_abort_other_queued_work(insights):
    insights.reg._x("INSERT INTO night_jobs(kind,payload,created_at) VALUES(?,?,?)", ("profile_proposal", "{broken", AT.isoformat()))
    submit(insights)
    result = insights.handle("nightly.run", {}, AT, closed=True)
    assert result[0] == "ok" and result[1]["jobs_failed"] == 1 and result[1]["jobs_run"] == 1
    assert len(dashboard(insights)["clusters"]) == 1
    assert sorted(row["status"] for row in dashboard(insights)["jobs"]) == ["done", "failed"]


def test_concurrent_retries_create_one_feedback_complaint_and_job(insights):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: submit(insights, "Плохо, не работает"), range(24)))
    assert all(result == results[0] for result in results)
    result = dashboard(insights)
    assert len(result["feedback"]) == len(result["complaints"]) == len(result["jobs"]) == 1


@pytest.mark.parametrize("arguments", [
    {"text": "x" * 4001, "call_id": "one"},
    {"text": "Текст", "call_id": ""},
    {"text": "Текст", "call_id": "x" * 129},
    {"text": "Текст", "call_id": "one", "registrar": "+"},
    {"text": "Текст", "call_id": "one", "registrar": "100;DROP TABLE feedback"},
    {"text": "Текст", "call_id": "one", "kind": "made_up"},
])
def test_invalid_input_never_partially_writes(insights, arguments):
    assert insights.handle("feedback.submit", arguments, AT)[0] == "error"
    assert dashboard(insights)["feedback"] == []


def test_similar_feedback_forms_deterministic_auditable_theme():
    rows = [{"id": 1, "text": "Говорите медленнее"}, {"id": 2, "text": "Пожалуйста говорите медленнее"}, {"id": 3, "text": "Неправильно распознаёт фамилию"}]
    result = clusters(rows)
    assert result == clusters(rows)
    assert any(group["feedback_ids"] == [1, 2] and group["title"] == "темп" for group in result)
    assert {fid for group in result for fid in group["feedback_ids"]} == {1, 2, 3}


def test_old_database_schema_upgrades_without_losing_feedback(tmp_path):
    reg = MockRegistry(str(tmp_path / "old.sqlite"), AT)
    reg._conn.executescript("CREATE TABLE feedback(id INTEGER PRIMARY KEY,call_id TEXT NOT NULL,registrar TEXT NOT NULL,text TEXT NOT NULL,kind TEXT NOT NULL,rating INTEGER,created_at TEXT NOT NULL,cluster_id INTEGER,UNIQUE(call_id,text)); CREATE TABLE feedback_clusters(id INTEGER PRIMARY KEY,title TEXT NOT NULL,terms TEXT NOT NULL,size INTEGER NOT NULL,updated_at TEXT NOT NULL);")
    reg._x("INSERT INTO feedback(call_id,registrar,text,kind,rating,created_at) VALUES(?,?,?,?,?,?)", ("old", "100", "Старый отзыв", "suggestion", 5, AT.isoformat()))
    service = Insights(reg)
    assert dashboard(service)["feedback"][0]["text"] == "Старый отзыв"
    assert dashboard(service)["metrics"][0]["explicit_rating_count"] == 0
    assert service.handle("nightly.run", {}, AT, closed=True)[1]["jobs_run"] == 0
    # Preserved legacy rows are queued when the next feedback arrives.
    submit(service, call="new")
    service.handle("nightly.run", {}, AT, closed=True)
    assert len(dashboard(service)["feedback"]) == 2
    assert all(row["cluster_id"] is not None for row in dashboard(service)["feedback"])


def test_international_phone_format_preserves_learning_scope(insights):
    phone = "+79990001122"
    learning = Learning(insights.reg)
    assert learning.handle("learning.archive", {"call_id": "intl", "registrar": phone}, AT)[0] == "ok"
    assert submit(insights, phone="+7 (999) 000-11-22", call="intl")[0] == "ok"
    result = dashboard(insights, {"role": "registrar", "registrar": phone})
    assert len(result["feedback"]) == 1 and result["feedback"][0]["registrar"] == phone


def test_cluster_batch_backlog_is_retained_and_processed_next_night(insights):
    with insights.reg._lock:
        insights.reg._conn.executemany(
            "INSERT INTO feedback(call_id,registrar,text,kind,created_at) VALUES(?,?,?,?,?)",
            [(f"bulk-{i}", "100", "Говорите медленнее", "suggestion", AT.isoformat()) for i in range(2001)],
        )
    insights._queue_cluster(AT)
    first = insights.handle("nightly.run", {}, AT, closed=True)
    assert first[1]["jobs_run"] == 1
    assert insights.reg.execute("SELECT COUNT(*) AS n FROM feedback WHERE cluster_id IS NULL")[0]["n"] == 1
    assert insights.reg.execute("SELECT COUNT(*) AS n FROM night_jobs WHERE status='queued'")[0]["n"] == 1
    second = insights.handle("nightly.run", {}, AT, closed=True)
    assert second[1]["jobs_run"] == 1
    assert insights.reg.execute("SELECT COUNT(*) AS n FROM feedback WHERE cluster_id IS NULL")[0]["n"] == 0
    assert insights.reg.execute("SELECT SUM(size) AS n FROM feedback_clusters")[0]["n"] == 2001

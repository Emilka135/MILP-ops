"""Local slot selection and explicitly simulated model envelopes; no paid calls."""

import json
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from calls.crm.registry import MockRegistry
from clinic import agent as agent_module
from clinic.agent import SlotAgent, decision_is_valid
from clinic.planner_store import SCHEMA, SQL, slots

AT = datetime(2026, 10, 16, 10, tzinfo=timezone(timedelta(hours=4)))


@pytest.fixture(autouse=True)
def no_external_model_or_crm(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "deterministic")

    def forbidden(*args, **kwargs):
        raise AssertionError("external requests are forbidden in fixture tests")

    monkeypatch.setattr(agent_module, "open_http", forbidden)
    monkeypatch.setattr(agent_module, "post_json", forbidden)


@pytest.fixture
def registry(tmp_path):
    reg = MockRegistry(str(tmp_path / "agent.sqlite"), AT.replace(tzinfo=None))
    reg._conn.executescript(SCHEMA)
    reg.add_patient(
        "SYNTHETIC_PATIENT_NAME",
        "PRIVATE_FIRST",
        "",
        datetime(1990, 1, 1).date(),
        "12345678901",
        "101",
        "79160000000",
    )
    reg.add_doctor("Доктор", "Доктор Тестовый", "терапевт", "1", False)
    reg.add_doctor("Аллерголог", "Аллерголог Тестовый", "аллерголог", "2", True)
    reg.add_doctor("Кардиолог", "Кардиолог Тестовый", "кардиолог", "3", False)
    return reg


def add(reg, stamp, doctor=1, status="free"):
    return reg._x(
        "INSERT INTO slots(doctor_id,starts_at,status) VALUES(?,?,?)",
        (doctor, stamp, status),
    )


def lookup(reg, **fields):
    return slots(reg, {"patient_id": 1, "specialty": "терапевт", **fields}, AT)


class LocalPlanner:
    def __init__(self, reg):
        self.reg = reg
        self.calls = []

    def call(self, op, args):
        self.calls.append((op, args))
        assert op == "agent_slots"
        return slots(self.reg, args, AT)


@pytest.mark.parametrize("constraint", ["daypart", "time", "excluded"])
def test_filtering_happens_before_three_day_selection(registry, constraint):
    reg = registry
    earlier = [add(reg, f"2026-10-{day:02}T08:00") for day in range(17, 20)]
    later = [add(reg, f"2026-10-{day:02}T17:30") for day in range(20, 23)]
    filters = {
        "daypart": {"said_daypart": "evening"},
        "time": {"said_time": "17:00"},
        "excluded": {"excluded": earlier},
    }[constraint]
    status, result = lookup(reg, **filters)
    assert status == "ok"
    assert [slot["id"] for slot in result["slots"]] == later
    assert len({row["starts_at"][:10] for row in result["slots"]}) == 3


def test_row_cap_does_not_hide_second_and_third_available_day(registry):
    reg = registry
    start = datetime(2026, 10, 17, 8)
    for minute in range(120):
        add(reg, (start + timedelta(minutes=minute)).isoformat(timespec="minutes"))
    second = add(reg, "2026-10-18T10:00")
    third = add(reg, "2026-10-19T10:00")
    fourth = add(reg, "2026-10-20T10:00")
    status, result = lookup(reg)
    assert status == "ok"
    ids = [row["id"] for row in result["slots"]]
    assert len(ids) == 34 and second in ids and third in ids and fourth not in ids


def test_custom_referral_policy_comes_from_doctor_data_not_specialty_list(registry):
    reg = registry
    allergy = add(reg, "2026-10-17T10:00", doctor=2)
    cardiology = add(reg, "2026-10-17T10:00", doctor=3)
    assert (
        lookup(reg, specialty="аллерголог")[1]["error"] == "referral_must_be_verified"
    )
    assert lookup(reg, specialty="аллерголог", referral_verified="false")[0] == "error"
    assert (
        lookup(reg, specialty="аллерголог", referral_verified=True)[1]["slots"][0]["id"]
        == allergy
    )
    assert lookup(reg, specialty="кардиолог")[1]["slots"][0]["id"] == cardiology


def test_assigned_doctor_and_mixed_referral_requirements_are_respected(registry):
    reg = registry
    ordinary = add(reg, "2026-10-17T10:00")
    referral_doctor = reg.add_doctor("Новый", "Новый Доктор", "терапевт", "4", True)
    referral_slot = add(reg, "2026-10-17T10:30", doctor=referral_doctor)
    assert [row["id"] for row in lookup(reg)[1]["slots"]] == [ordinary]
    reg._x("INSERT INTO patient_assignments VALUES(1,'терапевт',?)", (referral_doctor,))
    assert lookup(reg)[1]["error"] == "referral_must_be_verified"
    assert [row["id"] for row in lookup(reg, referral_verified=True)[1]["slots"]] == [
        referral_slot
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"said_date": "2026-10-18 SECRET_PATIENT"},
        {"said_date": "2026-02-30"},
        {"said_time": "25:10"},
        {"said_time": "9:5"},
        {"said_daypart": "SECRET_PATIENT"},
        {"excluded": [True]},
        {"excluded": ["1; DROP TABLE doctors"]},
        {"excluded": list(range(1, 102))},
        {"patient_id": True},
    ],
)
def test_malformed_constraints_fail_locally_before_query(registry, fields):
    assert lookup(registry, **fields) == (
        "error",
        {"error": "invalid_slot_constraints"},
    )


def test_model_sql_is_never_executed_and_values_stay_bound(registry):
    reg = registry
    add(reg, "2026-10-17T10:00")
    injected = "терапевт'; DROP TABLE doctors; --"
    assert lookup(reg, specialty=injected, sql="DROP TABLE patients")[1]["slots"] == []
    result = lookup(reg, sql="DELETE FROM doctors")[1]
    assert result["sql"] == SQL and result["params"]["specialty"] == "терапевт"
    assert len(reg.doctors()) == 3 and reg.patient_by_id(1) is not None
    assert reg.execute(result["sql"], result["params"]) == result["slots"]


def test_date_assignment_inactive_and_taken_slots_are_filtered(registry):
    reg = registry
    add(reg, "2026-10-17T10:00")
    add(reg, "2026-10-18T10:00", status="booked")
    target = add(reg, "2026-10-19T10:00")
    assert [row["id"] for row in lookup(reg, said_date="2026-10-18")[1]["slots"]] == [
        target
    ]
    reg._x("UPDATE doctors SET active=0 WHERE id=1")
    assert lookup(reg)[1]["slots"] == []


@pytest.mark.parametrize(
    "decision",
    [
        {"action": "operator", "slot_id": None, "text": None},
        {"action": "operator", "slot_id": None, "text": {"sql": "DROP"}},
        {"action": "operator", "slot_id": None, "text": "x" * 351},
        {"action": "operator", "slot_id": 1, "text": ""},
        {"action": "offer", "slot_id": True, "text": ""},
        {"action": "offer", "slot_id": 999, "text": ""},
        {"action": "offer", "slot_id": 1, "text": False},
        {"action": "offer", "slot_id": 1, "text": "", "sql": "DELETE FROM slots"},
    ],
)
def test_all_action_variants_require_exact_schema(decision):
    assert decision_is_valid(decision, [{"id": 1}]) is False


def test_deterministic_three_offers_are_distinct_and_do_not_write_bookings(registry):
    reg = registry
    for day in range(17, 21):
        add(reg, f"2026-10-{day}T10:00")
    planner = LocalPlanner(reg)
    agent = SlotAgent(planner)
    args = {"patient_id": 1, "specialty": "терапевт"}
    chosen = []
    for _ in range(3):
        status, result = agent.handle("offer", args)
        assert status == "ok" and result["agent_mode"] == "deterministic"
        assert result["agent_completed"] is False
        chosen.append(result["slot_id"])
        args.update(attempt=result["agent_attempt"], excluded=result["agent_excluded"])
    assert len(set(chosen)) == 3
    assert agent.handle("offer", args)[0] == "exhausted"
    assert len(planner.calls) == 3
    assert reg.execute("SELECT count(*) AS n FROM bookings")[0]["n"] == 0


def test_fixture_model_invalid_contract_retries_then_surfaces_failure(registry):
    add(registry, "2026-10-17T10:00")
    calls = []

    def malformed(slots, constraints):
        calls.append(1)
        return {"action": "operator", "slot_id": None, "text": []}

    agent = SlotAgent(LocalPlanner(registry), choose=malformed)
    status, result = agent.handle("offer", {"patient_id": 1, "specialty": "терапевт"})
    assert status == "error" and result["error"] == "model_contract_rejected"
    assert len(calls) == result["model_attempts"] == 2


def test_fixture_model_operator_decision_does_not_create_booking(registry):
    add(registry, "2026-10-17T10:00")
    agent = SlotAgent(
        LocalPlanner(registry),
        choose=lambda *_: {
            "action": "operator",
            "slot_id": None,
            "text": "Не найден подходящий вариант",
        },
    )
    assert agent.handle("offer", {"patient_id": 1, "specialty": "терапевт"}) == (
        "exhausted",
        {"reason": "model_cannot_offer"},
    )
    assert registry.execute("SELECT count(*) AS n FROM bookings")[0]["n"] == 0


def test_fixture_cannot_mutate_verified_slot_allowlist(registry):
    add(registry, "2026-10-17T10:00")

    def mutate_fixture(slots, constraints):
        slots[0]["id"] = 999
        return {"action": "offer", "slot_id": 999, "text": ""}

    agent = SlotAgent(LocalPlanner(registry), choose=mutate_fixture)
    assert (
        agent.handle("offer", {"patient_id": 1, "specialty": "терапевт"})[1]["error"]
        == "model_contract_rejected"
    )


def test_cloud_fixture_payload_contains_only_slot_ids_dates_and_normalized_constraints(
    registry, monkeypatch
):
    sid = add(registry, "2026-10-17T17:30")
    monkeypatch.setenv("AGENT_MODE", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key-fixture-only")
    monkeypatch.setenv("OPENROUTER_MODEL", "fixture-model-not-called")
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, limit):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "action": "offer",
                                        "slot_id": sid,
                                        "text": "Непроверенный выдуманный текст",
                                    }
                                )
                            }
                        }
                    ]
                }
            ).encode()

    def fake_http(request, timeout):
        seen.append((json.loads(request.data), timeout))
        return Response()

    monkeypatch.setattr(agent_module, "open_http", fake_http)
    agent = SlotAgent(LocalPlanner(registry))
    status, result = agent.handle(
        "offer",
        {
            "patient_id": 1,
            "specialty": "терапевт",
            "said_daypart": "evening",
            "said_time": "17:00",
            "patient_name": "SYNTHETIC_PATIENT_NAME",
            "phone": "79160000000",
            "snils": "12345678901",
            "birth_date": "1990-01-01",
            "utterance": "PRIVATE_UTTERANCE",
            "sql": "DROP TABLE patients",
            "history": "PRIVATE_HISTORY",
        },
    )
    assert status == "ok" and result["agent_mode"] == "openrouter"
    payload, timeout = seen[0]
    context = json.loads(payload["messages"][1]["content"])
    assert context["slots"] == [{"id": sid, "starts_at": "2026-10-17T17:30"}]
    assert context["constraints"] == {"said_time": "17:00", "said_daypart": "evening"}
    encoded = json.dumps(payload, ensure_ascii=False)
    for private in (
        "SYNTHETIC_PATIENT_NAME",
        "PRIVATE_FIRST",
        "79160000000",
        "12345678901",
        "1990-01-01",
        "PRIVATE_UTTERANCE",
        "PRIVATE_HISTORY",
        "DROP TABLE",
        "patient_id",
    ):
        assert private not in encoded
    assert payload["provider"]["data_collection"] == "deny"
    assert timeout <= agent_module.MODEL_TIMEOUT_SECONDS
    assert "Непроверенный" not in result["agent_text"]
    assert result["model_text"] == "Непроверенный выдуманный текст"
    assert payload["provider"]["require_parameters"] is True
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["schema"] == agent_module.SCHEMA


def test_provider_error_inside_http_success_is_visible_without_second_request(
    registry, monkeypatch
):
    add(registry, "2026-10-17T10:00")
    monkeypatch.setenv("AGENT_MODE", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-only-not-a-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "fixture-only-not-a-model")
    calls = []

    class ErrorResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return b'{"error":{"code":503,"message":"PRIVATE_PROVIDER_DETAILS"}}'

    def provider_error(*args, **kwargs):
        calls.append(1)
        return ErrorResponse()

    monkeypatch.setattr(agent_module, "open_http", provider_error)
    agent = SlotAgent(LocalPlanner(registry))
    status, result = agent.handle("offer", {"patient_id": 1, "specialty": "терапевт"})
    assert status == "error" and result["error"] == "model_provider_error"
    assert (
        result["provider_code"] == 503 and result["model_attempts"] == len(calls) == 1
    )
    assert "PRIVATE_PROVIDER_DETAILS" not in json.dumps(result)


@pytest.mark.parametrize(
    "exception,error",
    [
        (TimeoutError("private provider details"), "model_timeout"),
        (OSError("private endpoint or credential details"), "model_transport_error"),
        (
            urllib.error.HTTPError(
                "https://fixture.invalid", 429, "private body", {}, None
            ),
            "model_http_error",
        ),
    ],
)
def test_transport_failures_are_visible_without_retry_or_secret_details(
    registry, exception, error
):
    add(registry, "2026-10-17T10:00")
    calls = []

    def failing(*_):
        calls.append(1)
        raise exception

    agent = SlotAgent(LocalPlanner(registry), choose=failing)
    status, result = agent.handle("offer", {"patient_id": 1, "specialty": "терапевт"})
    assert status == "error" and result["error"] == error
    assert result["model_attempts"] == len(calls) == 1
    assert "private" not in json.dumps(result)


def test_total_wall_budget_bounds_two_contract_attempts(registry, monkeypatch):
    add(registry, "2026-10-17T10:00")
    monkeypatch.setattr(agent_module, "TOTAL_BUDGET_SECONDS", 0.07)
    monkeypatch.setattr(agent_module, "MODEL_TIMEOUT_SECONDS", 0.05)
    calls = []
    release = threading.Event()

    def slow(*_):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.04)
            return {"invalid": True}
        release.wait(timeout=1)
        return {"invalid": True}

    agent = SlotAgent(LocalPlanner(registry), choose=slow)
    start = time.monotonic()
    try:
        status, result = agent.handle(
            "offer", {"patient_id": 1, "specialty": "терапевт"}
        )
        elapsed = time.monotonic() - start
    finally:
        release.set()
    assert status == "error" and result["error"] == "model_timeout"
    assert len(calls) == 2 and elapsed < 0.25


def test_crm_wall_budget_is_bounded_before_model_attempt(monkeypatch):
    monkeypatch.setattr(agent_module, "CRM_TIMEOUT_SECONDS", 0.025)
    release = threading.Event()

    class SlowPlanner:
        def call(self, *_):
            release.wait(timeout=1)
            return "ok", {"slots": []}

    agent = SlotAgent(
        SlowPlanner(), choose=lambda *_: pytest.fail("model must not run")
    )
    try:
        started = time.monotonic()
        status, result = agent.handle(
            "offer", {"patient_id": 1, "specialty": "терапевт"}
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert status == "error" and result["error"] == "crm_timeout"
    assert elapsed < 0.2


def test_default_crm_proxy_uses_agent_budget_instead_of_ninety_seconds(
    registry, monkeypatch
):
    sid = add(registry, "2026-10-17T10:00")
    observed = []

    def fake_post(url, body, timeout):
        observed.append(timeout)
        status, payload = slots(registry, body, AT)
        return {"status": status, "payload": payload}

    monkeypatch.setattr(agent_module, "post_json", fake_post)
    agent = SlotAgent(agent_module.CrmProxy("http://fixture.invalid"))
    assert (
        agent.handle("offer", {"patient_id": 1, "specialty": "терапевт"})[1]["slot_id"]
        == sid
    )
    assert observed == [agent_module.CRM_TIMEOUT_SECONDS]


@pytest.mark.parametrize("patient", [1.0, 1.9, "1.0", "-1", 0, -1, None])
def test_patient_identifiers_are_never_lossily_coerced(registry, patient):
    assert lookup(registry, patient_id=patient) == (
        "error", {"error": "invalid_slot_constraints"}
    )


@pytest.mark.parametrize("args", [None, [], "specialty", {"specialty": "терапевт", "attempt": []}])
def test_agent_rejects_non_object_or_invalid_attempt_before_crm(args):
    class NoCrm:
        def call(self, *_):
            pytest.fail("invalid args must not reach CRM")

    assert SlotAgent(NoCrm()).handle("offer", args)[1]["error"] == "invalid_agent_constraints"


@pytest.mark.parametrize("mutation", [
    lambda row: {**row, "starts_at": "2026-99-99T12:00"},
    lambda row: {**row, "starts_at": "2026-10-17T12:00+03:00"},
    lambda row: {**row, "id": True},
])
def test_corrupt_crm_facts_never_reach_model(registry, mutation):
    add(registry, "2026-10-17T10:00")
    class CorruptPlanner(LocalPlanner):
        def call(self, op, args):
            status, result = super().call(op, args)
            result["slots"] = [mutation(result["slots"][0])]
            return status, result
    agent = SlotAgent(CorruptPlanner(registry), choose=lambda *_: pytest.fail("invalid CRM facts"))
    assert agent.handle("offer", {"patient_id": 1, "specialty": "терапевт"})[1]["error"] == "invalid_slot_response"


def test_openai_compatible_gateway_uses_same_private_structured_contract(registry, monkeypatch, tmp_path):
    sid = add(registry, "2026-10-17T10:00")
    keyfile = tmp_path / "fixture.key"
    keyfile.write_text("fixture-only-local-key\n")
    monkeypatch.setenv("AGENT_MODE", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(keyfile))
    monkeypatch.setenv("OPENAI_MODEL", "fixture-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.invalid/api/v1/llm/v1/")
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _):
            return json.dumps({"choices": [{"message": {"content": json.dumps({"action": "offer", "slot_id": sid, "text": "UNTRUSTED_FACTS"})}}]}).encode()
    def fake_http(request, timeout):
        seen.append(request)
        return Response()
    monkeypatch.setattr(agent_module, "open_http", fake_http)
    result = SlotAgent(LocalPlanner(registry)).handle("offer", {
        "patient_id": 1, "specialty": "терапевт", "patient_name": "PRIVATE_NAME", "history": "PRIVATE_HISTORY"
    })[1]
    request = seen[0]
    assert request.full_url == "https://gateway.invalid/api/v1/llm/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer fixture-only-local-key"
    payload = json.loads(request.data)
    assert "provider" not in payload
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "PRIVATE_" not in json.dumps(payload)
    assert result["agent_mode"] == "openai" and result["agent_completed"] is False
    assert "UNTRUSTED_FACTS" not in result["agent_text"]


@pytest.mark.parametrize("url", [
    "http://gateway.invalid/v1", "https://secret@gateway.invalid/v1",
    "https://gateway.invalid/v1?key=secret", "https://gateway.invalid/v1#secret",
    "file:///tmp/key", "http://127.0.0.1:8001/v1"
])
def test_gateway_url_cannot_leak_credentials_to_plain_http_or_url(url, monkeypatch):
    monkeypatch.delenv("ALLOW_LOCAL_LLM", raising=False)
    with pytest.raises(ValueError):
        agent_module.completion_url(url)


def test_local_gateway_requires_explicit_administrator_opt_in(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_LLM", "1")
    assert agent_module.completion_url("http://127.0.0.1:8001/v1/") == "http://127.0.0.1:8001/v1/chat/completions"


def test_provider_credentials_are_never_silently_borrowed(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "openai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-only-other-provider-key")
    monkeypatch.setenv("OPENAI_MODEL", "fixture-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_FILE", raising=False)
    with pytest.raises(ValueError, match="OPENAI_MODEL and local API key"):
        SlotAgent()


def test_configuration_writes_private_ignored_key_and_rejects_multiline(tmp_path):
    from tools.configure_openrouter import save_key
    path = save_key("openai", "fixture-only-private-key", root=tmp_path)
    assert path == tmp_path / "secrets" / "openai.key"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == "fixture-only-private-key\n"
    with pytest.raises(ValueError):
        save_key("openai", "first\nsecond", root=tmp_path)
    assert path.read_text() == "fixture-only-private-key\n"


@pytest.mark.parametrize("acknowledgement", ["correct", "wrong_time", "wrong_doctor", "timeout"])
def test_proposed_slot_never_means_booked_without_matching_remote_ack(registry, monkeypatch, acknowledgement):
    """Real ClinicCrm mutation logic, synthetic remote API; zero live network calls."""
    from clinic.crm import ClinicCrm
    from app.types import MisTimeout

    monkeypatch.setenv("CRM_NOW", AT.isoformat())
    sid = add(registry, "2026-10-17T10:00")
    calls = []
    class RemoteFixture:
        def request(self, method, path, body=None):
            calls.append((method, path, body))
            if path == "/clients": return {"id": 42}
            assert path == "/visits"
            assert registry.execute("SELECT count(*) AS n FROM bookings")[0]["n"] == 0
            if acknowledgement == "timeout":
                raise MisTimeout("fixture response lost", 0, "TIMEOUT")
            return {
                "id": 101, "status": "Запланирована",
                "scheduled_at": "17.10.2026 11:00" if acknowledgement == "wrong_time" else "17.10.2026 10:00",
                "doctor": {"id": 99 if acknowledgement == "wrong_doctor" else 1},
            }
    crm = ClinicCrm(registry, RemoteFixture())
    # Synthetic API contract test: explicitly attest the fixture occupancy snapshot.
    crm._last_slots_sync = time.monotonic()
    class Proxy:
        def call(self, op, args): return crm.handle(op, args)
    status, offer = SlotAgent(Proxy()).handle("offer", {"patient_id": 1, "specialty": "терапевт"})
    assert status == "ok" and offer["agent_completed"] is False and calls == []
    assert offer["slot_id"] == sid
    status, result = crm.handle("book", {"operation_key": "fixture-book", "patient_id": 1, "slot_id": sid})
    if acknowledgement == "correct":
        assert status == "ok" and result["stand_acknowledged"] is True
        assert result["booking_status"] == "booked" and registry.remote_id_of(result["booking_id"]) == "101"
    else:
        assert status == "error" and result["operation_state"] == "uncertain"
        assert registry.execute("SELECT count(*) AS n FROM bookings")[0]["n"] == 0
        assert registry.execute("SELECT status FROM slots WHERE id=?", (sid,))[0]["status"] == "reserved"
    assert [path for _, path, _ in calls] == ["/clients", "/visits"]


def test_explicit_gratitude_has_sufficient_confidence_without_overriding_request():
    from clinic.modules import Nlu
    nlu = Nlu()
    for text in ("Спасибо", "Спасибо большое!", "Всего доброго.", "До свидания"):
        status, payload = nlu.handle("interpret", {"text": text, "today": "2026-10-15"})
        assert status == "ok" and payload["intent"] == "gratitude" and payload["confidence"] >= 0.6
    for text in ("Спасибо, но отмените запись", "Спасибо, позовите оператора"):
        assert nlu.handle("interpret", {"text": text})[1]["intent"] != "gratitude"

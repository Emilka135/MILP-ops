"""Unattended night work has independent, bounded, privacy-safe requests."""
from datetime import datetime, timedelta, timezone
import io
import json

import pytest

from calls.crm.registry import MockRegistry
from clinic.crm import ClinicCrm
from clinic import nightly

AT = datetime(2026, 10, 19, 10, tzinfo=timezone(timedelta(hours=4)))


@pytest.fixture
def crm(tmp_path):
    service = ClinicCrm(MockRegistry(str(tmp_path / "night.sqlite"), AT))
    learning = service.learning
    assert learning.handle("learning.archive", {"call_id": "sample", "registrar": "100", "trace": ["inbound.welcome", "inbound.listen_free"]}, AT)[0] == "ok"
    assert learning.handle("learning.propose", {"call_id": "sample", "registrar": "100", "source_state": "inbound.listen_free", "title": "Спросить раньше", "intent": "book", "examples": ["Запишите меня"], "response": "К кому хотите записаться?"}, AT)[0] == "ok"
    assert service.insights.handle("feedback.submit", {"call_id": "sample", "registrar": "100", "text": "Говорите медленнее"}, AT)[0] == "ok"
    return service


class LocalProxy:
    def __init__(self, crm):
        self.crm = crm
        self.calls = []

    def call(self, op, args):
        self.calls.append((op, args))
        return self.crm.handle(op, args)


def logs(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_both_queues_remain_pending_during_shared_opening_hours(crm, monkeypatch, capsys):
    monkeypatch.setenv("CRM_NOW", AT.isoformat())
    proxy = LocalProxy(crm)
    status, result = nightly.run_once(proxy)
    assert status == "blocked" and result["processed"] == 0
    assert proxy.calls == [("learning.night", {}), ("nightly.run", {})]
    output = logs(capsys)
    assert len(output) == 2 and all(row["status"] == "blocked" and row["reason"] == "clinic_is_open" for row in output)
    assert crm.reg.execute("SELECT status FROM learning_cases")[0]["status"] == "queued"
    assert crm.reg.execute("SELECT status FROM night_jobs")[0]["status"] == "queued"
    assert crm.reg.execute("SELECT * FROM feedback_clusters") == []


def test_both_queues_process_after_closing_without_activating(crm, monkeypatch, capsys):
    monkeypatch.setenv("CRM_NOW", AT.replace(hour=21).isoformat())
    proxy = LocalProxy(crm)
    status, result = nightly.run_once(proxy)
    assert status == "ok" and result["processed"] == 1
    output = logs(capsys)
    assert [row["status"] for row in output] == ["ok", "ok"]
    assert output[1]["jobs_run"] == 1
    assert crm.reg.execute("SELECT status FROM learning_cases")[0]["status"] == "prepared"
    assert crm.reg.execute("SELECT status FROM night_jobs")[0]["status"] == "done"
    assert len(crm.reg.execute("SELECT * FROM feedback_clusters")) == 1
    nightly.run_once(proxy)
    output = logs(capsys)
    assert output[0]["processed"] == 0 and output[1]["jobs_run"] == 0


def test_calendar_exception_controls_both_queues_even_in_daytime(crm, monkeypatch, capsys):
    monkeypatch.setenv("CRM_NOW", AT.isoformat())
    crm.reg._x("INSERT INTO service_calendar VALUES(?,?,?,?,?,?)", ("registrar", AT.date().isoformat(), None, None, 1, "synthetic closure"))
    assert nightly.run_once(LocalProxy(crm))[0] == "ok"
    assert [row["status"] for row in logs(capsys)] == ["ok", "ok"]


@pytest.mark.parametrize("failed_op", ["learning.night", "nightly.run"])
@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError])
def test_one_failure_never_skips_other_queue_and_does_not_log_secrets(failed_op, failure, capsys):
    secret = "TOKEN-private-patient-phone-79999999999"
    class Proxy:
        def __init__(self):
            self.calls = []
        def call(self, op, args):
            self.calls.append(op)
            if op == failed_op:
                raise failure(secret)
            return "ok", {"processed": 7, "jobs_run": 3, "text": secret, "url": secret}
    proxy = Proxy()
    status, result = nightly.run_once(proxy)
    output = logs(capsys)
    assert proxy.calls == ["learning.night", "nightly.run"]
    assert output[0 if failed_op == "learning.night" else 1]["status"] == "error"
    assert output[1 if failed_op == "learning.night" else 0]["status"] == "ok"
    assert secret not in json.dumps(output) and secret not in json.dumps(result)
    assert status == ("error" if failed_op == "learning.night" else "ok")


@pytest.mark.parametrize("bad_response", [None, ("ok", None), ("unknown", {}), ("ok", {}, {}), "secret-patient-token"])
def test_malformed_first_response_does_not_skip_feedback(bad_response, capsys):
    class Proxy:
        def call(self, op, args):
            return bad_response if op == "learning.night" else ("ok", {"jobs_run": 1})
    assert nightly.run_once(Proxy()) == ("error", {"error": "invalid_rpc_response"})
    assert logs(capsys)[1]["jobs_run"] == 1


def test_logs_have_fixed_bounded_fields_and_no_backend_error_text(capsys):
    class Proxy:
        def call(self, op, args):
            return "error", {"reason": "secret" * 100000, "error": "secret", "processed": -1, "jobs_run": True, "jobs_failed": 10**100, "transcript": "secret"}
    nightly.run_once(Proxy())
    raw = capsys.readouterr().out
    assert len(raw) < 512 and "secret" not in raw
    assert len(raw.splitlines()) == 2
    assert all(json.loads(row)["error"] == "crm_job_rejected" for row in raw.splitlines())


class Response(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *unused):
        self.close()


def test_default_worker_proxy_caps_response_and_sets_timeout(monkeypatch):
    seen = {}
    def response(request, timeout):
        seen.update(url=request.full_url, data=request.data, timeout=timeout)
        return Response(json.dumps({"status": "ok", "payload": {"processed": 2}}).encode())
    monkeypatch.setattr(nightly, "open_http", response)
    proxy = nightly.NightCrmProxy("http://crm:8081")
    assert proxy.call("learning.night", {}) == ("ok", {"processed": 2})
    assert seen == {"url": "http://crm:8081/v1/learning.night", "data": b"{}", "timeout": 30}
    monkeypatch.setattr(nightly, "open_http", lambda *args, **kwargs: Response(b" " * (nightly.MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(ValueError, match="exceeded limit"):
        proxy.call("nightly.run", {})
    with pytest.raises(ValueError, match="invalid nightly operation"):
        proxy.call("feedback.submit", {"text": "injected"})


def test_worker_forever_loop_uses_bounded_proxy_and_fixed_interval(monkeypatch, capsys):
    class Proxy:
        def call(self, op, args):
            return "ok", {"processed": 0, "jobs_run": 0}
    proxy = Proxy()
    monkeypatch.setattr(nightly, "NightCrmProxy", lambda: proxy)
    def stop(seconds):
        assert seconds == 60
        raise KeyboardInterrupt
    monkeypatch.setattr(nightly.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        nightly.run()
    assert len(logs(capsys)) == 2

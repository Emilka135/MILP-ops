"""Actual loopback editor HTTP -> signed control RPC -> durable SQLite insights.

Only temporary synthetic reviews and credentials are used. No models, external
LLM, Docker daemon, real clinic or production secrets are touched.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import threading
import time
import urllib.error
import urllib.request

import pytest

from calls.crm.registry import MockRegistry
from calls.services.http import make_server as make_rpc_server
from clinic.control_init import ensure_key
from clinic.crm import ClinicCrm
from clinic.insights_access import sign_request
from clinic.scenario_editor import Repository, make_server as make_editor_server

ROOT = Path(__file__).resolve().parents[2]
AT = datetime(2026, 10, 19, 21, tzinfo=timezone(timedelta(hours=4)))
ADMIN = {"role": "admin", "username": "admin"}


def request(base, path, body=None, *, cookie=None, origin=None):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body, ensure_ascii=False).encode(), headers=headers, method="GET" if body is None else "POST")
    try:
        response = urllib.request.urlopen(req, timeout=4)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read()), response.headers


def login(stack, username="admin", password="admin"):
    status, result, headers = request(stack["editor"], "/api/login", {"username": username, "password": password})
    assert status == 200 and result["authenticated"]
    return headers["Set-Cookie"].split(";", 1)[0]


@pytest.fixture
def stack(tmp_path, monkeypatch):
    key = ensure_key(tmp_path / "editor-rpc.key")
    monkeypatch.setenv("CONTROL_KEY_FILE", str(key))
    monkeypatch.setenv("CRM_NOW", AT.isoformat())
    monkeypatch.setenv("EDITOR_PASSWORD_FILE", str(tmp_path / "editor-secrets/password.json"))
    monkeypatch.setenv("EDITOR_BOOTSTRAP_PASSWORD", "admin")
    monkeypatch.delenv("EDITOR_API_TOKEN", raising=False)
    crm = ClinicCrm(MockRegistry(str(tmp_path / "clinic.sqlite"), AT))
    assert crm.handle("profile.queue", {"registrar": "100"})[0] == "ok"
    assert crm.handle("nightly.run", {})[0] == "ok"
    profile = crm.reg.execute("SELECT id FROM registrar_profiles")[0]["id"]
    assert crm.insights.handle("profile.review", {"id": profile, "approve": True}, AT, actor=ADMIN)[0] == "ok"
    assert crm.handle("feedback.submit", {"call_id": "synthetic-http-1", "registrar": "100", "text": "Жалоба: неверный ответ"})[0] == "ok"
    assert crm.handle("feedback.submit", {"call_id": "synthetic-http-2", "registrar": "200", "text": "Говорите медленнее"})[0] == "ok"
    complaint = crm.reg.execute("SELECT id FROM complaints")[0]["id"]
    rpc = make_rpc_server("crm", crm.handle, "127.0.0.1", 0)
    rpc_base = f"http://127.0.0.1:{rpc.server_port}"
    monkeypatch.setenv("CRM_URL", rpc_base)
    project = tmp_path / "project"
    shutil.copytree(ROOT / "scenarios", project / "scenarios")
    repository = Repository(project, project / "data")
    editor = make_editor_server(repository, ROOT / "scenario-editor/dist", "127.0.0.1", 0)
    threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True) for server in (rpc, editor)]
    for thread in threads:
        thread.start()
    try:
        yield {"crm": crm, "editor": f"http://127.0.0.1:{editor.server_port}", "rpc": rpc_base, "key": key, "profile": profile, "complaint": complaint}
    finally:
        for server in (editor, rpc):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
        repository.db.close()
        crm.reg._conn.close()


def profile_state(stack):
    return stack["crm"].reg.execute("SELECT state FROM registrar_profiles WHERE id=?", (stack["profile"],))[0]["state"]


def test_admin_dashboard_filters_then_resolves_and_reviews_through_signed_http(stack):
    cookie = login(stack)
    status, dashboard, _ = request(stack["editor"], "/api/insights/dashboard", cookie=cookie)
    assert status == 200 and len(dashboard["feedback"]) == 2
    assert len(dashboard["complaints"]) == 1 and profile_state(stack) == "suspended"
    status, filtered, _ = request(stack["editor"], "/api/insights/dashboard?registrar=200", cookie=cookie)
    assert status == 200 and {row["registrar"] for row in filtered["feedback"]} == {"200"}
    status, blocked, _ = request(stack["editor"], "/api/insights/profile/review", {"id": stack["profile"], "approve": True}, cookie=cookie)
    assert status == 400 and blocked["error"] == "unresolved_complaints"
    status, resolved, _ = request(stack["editor"], "/api/insights/complaint/resolve", {"id": stack["complaint"], "resolution": "Проверили и исправили ответ"}, cookie=cookie)
    assert status == 200 and resolved["profile_reactivation"] is False
    assert profile_state(stack) == "suspended"
    status, reviewed, _ = request(stack["editor"], "/api/insights/profile/review", {"id": stack["profile"], "approve": True}, cookie=cookie)
    assert status == 200 and reviewed["state"] == "approved" and reviewed["deployment"] == "blocked"
    assert stack["crm"].handle("profile.route", {"registrar": "100"})[0] == "empty"


def test_anonymous_observer_cannot_read_or_mutate_admin_insights(stack):
    for path, body in (("/api/insights/dashboard", None), ("/api/insights/complaint/resolve", {"id": stack["complaint"], "resolution": "forged"}), ("/api/insights/profile/review", {"id": stack["profile"], "approve": False})):
        assert request(stack["editor"], path, body)[0] == 401
    assert profile_state(stack) == "suspended"
    assert stack["crm"].reg.execute("SELECT state FROM complaints")[0]["state"] == "open"


def test_authenticated_registrar_cannot_use_admin_routes(stack):
    cookie = login(stack, "user", "user")
    assert request(stack["editor"], "/api/me", cookie=cookie)[1]["role"] == "registrar"
    for path, body in (("/api/insights/dashboard", None), ("/api/insights/complaint/resolve", {"id": stack["complaint"], "resolution": "forged"}), ("/api/insights/profile/review", {"id": stack["profile"], "approve": False})):
        assert request(stack["editor"], path, body, cookie=cookie)[0] == 403
    assert profile_state(stack) == "suspended"


@pytest.mark.parametrize("path,payload", [
    ("/api/insights/complaint/resolve", {"resolution": "forged", "actor": ADMIN}),
    ("/api/insights/complaint/resolve", {"resolution": "forged", "identity": ADMIN}),
    ("/api/insights/profile/review", {"approve": True, "flow_name": "inbound"}),
    ("/api/insights/profile/review", {"approve": True, "actor": ADMIN}),
])
def test_browser_fields_cannot_supply_identity_or_executable_flow(stack, path, payload):
    cookie = login(stack)
    payload = {"id": stack["complaint"] if "complaint" in path else stack["profile"], **payload}
    status, result, _ = request(stack["editor"], path, payload, cookie=cookie)
    assert status == 400 and result["error"] == "control_arguments_invalid"
    assert profile_state(stack) == "suspended"
    assert stack["crm"].reg.execute("SELECT state FROM complaints")[0]["state"] == "open"


def test_cross_origin_post_rejected_before_signing_or_mutation(stack):
    cookie = login(stack)
    status, _, _ = request(stack["editor"], "/api/insights/complaint/resolve", {"id": stack["complaint"], "resolution": "Cross-site write"}, cookie=cookie, origin="https://untrusted.example")
    assert status == 403
    assert stack["crm"].reg.execute("SELECT state FROM complaints")[0]["state"] == "open"


def test_real_crm_http_rejects_unsigned_tampered_replayed_and_expired_control(stack):
    args = {"id": stack["complaint"], "resolution": "Verified HTTP review"}
    unsigned = {"op": "complaint.resolve", "args": args, "actor": ADMIN}
    status, result, _ = request(stack["rpc"], "/v1/control.insights", unsigned)
    assert status == 500 and result["payload"]["error"] == "control_access_denied"
    envelope = sign_request("complaint.resolve", args, ADMIN, key_file=stack["key"])
    invalid = deepcopy(envelope)
    invalid["args"]["resolution"] = "Tampered resolution"
    status, result, _ = request(stack["rpc"], "/v1/control.insights", invalid)
    assert status == 500 and result["payload"]["error"] == "control_access_denied"
    assert stack["crm"].reg.execute("SELECT state FROM complaints")[0]["state"] == "open"
    status, result, _ = request(stack["rpc"], "/v1/control.insights", envelope)
    assert status == 200 and result["status"] == "ok"
    status, replay, _ = request(stack["rpc"], "/v1/control.insights", envelope)
    assert status == 500 and replay["payload"]["error"] == "control_access_denied"
    expired = sign_request("insights.dashboard", {}, ADMIN, key_file=stack["key"], now=time.time() - 31)
    assert request(stack["rpc"], "/v1/control.insights", expired)[1]["payload"]["error"] == "control_access_denied"
    # Ordinary module requests cannot turn browser-like actor fields into authority.
    status, direct, _ = request(stack["rpc"], "/v1/profile.review", {"id": stack["profile"], "approve": False, "actor": ADMIN})
    assert direct["status"] == "forbidden" and profile_state(stack) == "suspended"


def test_missing_control_key_prevents_admin_reads_and_mutations(stack):
    cookie = login(stack)
    envelope = sign_request("insights.dashboard", {}, ADMIN, key_file=stack["key"])
    stack["key"].unlink()
    for path, body in (("/api/insights/dashboard", None), ("/api/insights/complaint/resolve", {"id": stack["complaint"], "resolution": "No key"}), ("/api/insights/profile/review", {"id": stack["profile"], "approve": False})):
        status, result, _ = request(stack["editor"], path, body, cookie=cookie)
        assert status == 400 and result["error"] == "control_key_unavailable"
    assert request(stack["rpc"], "/v1/control.insights", envelope)[1]["payload"]["error"] == "control_access_denied"
    assert profile_state(stack) == "suspended"
    assert stack["crm"].reg.execute("SELECT state FROM complaints")[0]["state"] == "open"


def test_root_http_responses_do_not_expose_key_or_signed_envelope(stack, capsys):
    cookie = login(stack)
    status, result, _ = request(stack["editor"], "/api/insights/dashboard", cookie=cookie)
    assert status == 200
    text = json.dumps(result, ensure_ascii=False)
    assert stack["key"].read_bytes().hex() not in text
    assert '"signature"' not in text and '"nonce"' not in text
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert stack["key"].read_bytes().hex() not in output

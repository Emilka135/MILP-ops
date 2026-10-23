"""Internal admin requests authenticate every field and reject replay fail-closed."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from clinic.insights_access import AccessError, ReplayCache, sign_request, verify_request
from clinic.insights import Insights
from calls.crm.registry import MockRegistry
from datetime import datetime, timezone

ADMIN = {"role": "admin", "username": "admin", "registrar": None, "label": "Администратор"}


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "control.key"
    path.write_bytes(os.urandom(32))
    path.chmod(0o600)
    return path


def signed(key, op="insights.dashboard", args=None, **kwargs):
    return sign_request(op, {} if args is None else args, ADMIN, key_file=key, now=1000, **kwargs)


def verified(key, envelope, **kwargs):
    return verify_request(envelope, key_file=key, now=1001, replay_cache=ReplayCache(), **kwargs)


@pytest.mark.parametrize("op,args", [
    ("insights.dashboard", {}),
    ("insights.dashboard", {"registrar": "+79990001122"}),
    ("complaint.resolve", {"id": 17, "resolution": "Ошибка проверена и исправлена"}),
    ("profile.review", {"id": 9, "approve": True}),
    ("profile.review", {"id": 9, "approve": False}),
])
def test_round_trip_preserves_exact_operation_arguments_and_only_derived_claims(key, op, args):
    envelope = signed(key, op, args)
    assert verified(key, envelope) == (op, args, {"role": "admin", "username": "admin"})
    assert envelope["actor"] == {"role": "admin", "username": "admin"}
    assert len(json.dumps(envelope, ensure_ascii=False).encode()) < 16384
    assert key.read_bytes().hex() not in json.dumps(envelope)


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(op="profile.review", args={"id": 1, "approve": True}),
    lambda e: e["args"].update(registrar="200"),
    lambda e: e["actor"].update(username="other_admin"),
    lambda e: e.update(timestamp=999),
    lambda e: e.update(nonce="b" * 32),
    lambda e: e.update(signature="0" * 64),
])
def test_each_authenticated_field_rejects_tampering(key, mutation):
    envelope = signed(key)
    mutation(envelope)
    with pytest.raises(AccessError):
        verified(key, envelope)


def test_json_object_key_order_does_not_change_authenticated_semantics(key):
    original = signed(key, "complaint.resolve", {"resolution": "Проверено", "id": 1})
    reordered = dict(reversed(list(original.items())))
    reordered["args"] = dict(reversed(list(original["args"].items())))
    assert verified(key, reordered)[0] == "complaint.resolve"


def test_replay_is_rejected_without_evicting_live_nonces(key):
    cache = ReplayCache(max_entries=1)
    first = signed(key)
    assert verify_request(first, key_file=key, now=1000, replay_cache=cache)[0] == "insights.dashboard"
    with pytest.raises(AccessError, match="replayed"):
        verify_request(first, key_file=key, now=1001, replay_cache=cache)
    with pytest.raises(AccessError, match="capacity"):
        verify_request(signed(key), key_file=key, now=1001, replay_cache=cache)
    with pytest.raises(AccessError, match="replayed"):
        verify_request(first, key_file=key, now=1001, replay_cache=cache)
    later = sign_request("insights.dashboard", {}, ADMIN, key_file=key, now=1031)
    assert verify_request(later, key_file=key, now=1031, replay_cache=cache)[0] == "insights.dashboard"


def test_concurrent_duplicate_executes_once(key):
    cache = ReplayCache()
    envelope = signed(key)
    def attempt(_):
        try:
            verify_request(envelope, key_file=key, now=1001, replay_cache=cache)
            return True
        except AccessError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(24))) == 1


def test_invalid_signature_does_not_consume_nonce(key):
    cache = ReplayCache()
    original = signed(key)
    invalid = deepcopy(original)
    invalid["signature"] = "0" * 64
    with pytest.raises(AccessError):
        verify_request(invalid, key_file=key, now=1001, replay_cache=cache)
    assert verify_request(original, key_file=key, now=1001, replay_cache=cache)[0] == "insights.dashboard"


@pytest.mark.parametrize("now", [999.9, 1030.1, 2000])
def test_future_and_expired_requests_are_rejected(key, now):
    with pytest.raises(AccessError, match="expired"):
        verify_request(signed(key), key_file=key, now=now, replay_cache=ReplayCache())


def test_exact_ttl_boundary_is_valid(key):
    assert verify_request(signed(key), key_file=key, now=1030, replay_cache=ReplayCache())[0] == "insights.dashboard"


@pytest.mark.parametrize("identity", [None, {}, {"role": "observer", "username": "observer"}, {"role": "registrar", "username": "user"}, {"role": "admin", "username": "../../admin"}])
def test_signing_requires_authenticated_admin_identity(key, identity):
    with pytest.raises(AccessError):
        sign_request("insights.dashboard", {}, identity, key_file=key, now=1000)


@pytest.mark.parametrize("op,args", [
    ("nightly.run", {}),
    ("profile.route", {"registrar": "100"}),
    ("profile.queue", {"registrar": "100"}),
    ("insights.dashboard", {"actor": ADMIN}),
    ("insights.dashboard", {"registrar": "100;DROP TABLE feedback"}),
    ("complaint.resolve", {"id": 1, "resolution": ""}),
    ("complaint.resolve", {"id": True, "resolution": "Да"}),
    ("complaint.resolve", {"id": -1, "resolution": "Да"}),
    ("profile.review", {"id": 1, "approve": "true"}),
    ("profile.review", {"id": 1, "approve": True, "flow_name": "inbound"}),
    ("profile.review", {"id": 1, "approve": True, "actor": ADMIN}),
    ([], {}),
])
def test_signer_rejects_unpermitted_operations_and_ambiguous_arguments(key, op, args):
    with pytest.raises(AccessError):
        signed(key, op, args)


@pytest.mark.parametrize("patch", [
    {"extra": "field"}, {"version": True}, {"timestamp": True}, {"actor": {"role": "admin", "username": "admin", "registrar": "100"}},
    {"actor": {"role": "registrar", "username": "user"}}, {"nonce": "n"}, {"signature": "bad"}, {"op": []},
])
def test_verifier_rejects_malformed_envelopes_before_dispatch(key, patch):
    envelope = signed(key)
    envelope.update(patch)
    with pytest.raises(AccessError):
        verified(key, envelope)


def test_wrong_key_and_unsafe_key_file_fail_closed(key, tmp_path):
    envelope = signed(key)
    other = tmp_path / "other.key"
    other.write_bytes(os.urandom(32))
    other.chmod(0o444)
    with pytest.raises(AccessError, match="signature"):
        verify_request(envelope, key_file=other, now=1001)
    key.chmod(0o666)
    with pytest.raises(AccessError, match="permissions"):
        signed(key)
    key.chmod(0o600)
    link = tmp_path / "link.key"
    link.symlink_to(key)
    with pytest.raises(AccessError, match="unavailable"):
        signed(link)


@pytest.mark.parametrize("size", [0, 16, 31, 33, 64])
def test_key_has_exactly_32_random_bytes(key, size):
    key.write_bytes(b"k" * size)
    with pytest.raises(AccessError, match="format"):
        signed(key)


def test_readonly_shared_key_and_local_env_path_are_supported(key, monkeypatch):
    key.chmod(0o444)
    monkeypatch.setenv("CONTROL_KEY_FILE", str(key))
    envelope = sign_request("insights.dashboard", {}, ADMIN, now=1000)
    assert verify_request(envelope, now=1001, replay_cache=ReplayCache())[0] == "insights.dashboard"


def test_unavailable_key_message_exposes_no_path_or_contents(tmp_path):
    secret_path = tmp_path / "private-token-value"
    with pytest.raises(AccessError) as caught:
        sign_request("insights.dashboard", {}, ADMIN, key_file=secret_path, now=1000)
    assert str(caught.value) == "control_key_unavailable"


def test_large_escaped_json_body_fails_closed(key):
    with pytest.raises(AccessError, match="too_large"):
        signed(key, "complaint.resolve", {"id": 1, "resolution": "\x00" * 4000})


def test_verified_actor_enables_only_real_backend_admin_action(key, tmp_path):
    at = datetime(2026, 10, 19, 21, tzinfo=timezone.utc)
    insights = Insights(MockRegistry(str(tmp_path / "clinic.sqlite"), at))
    status, review = insights.handle("feedback.submit", {"call_id": "synthetic", "registrar": "100", "text": "У меня жалоба: неверный ответ"}, at)
    assert status == "ok"
    complaint = insights.reg.execute("SELECT id FROM complaints WHERE feedback_id=?", (review["feedback_id"],))[0]
    args = {"id": complaint["id"], "resolution": "Ответ исправлен после проверки"}
    assert insights.handle("complaint.resolve", {**args, "actor": ADMIN}, at)[0] == "forbidden"
    op, verified_args, actor = verified(key, signed(key, "complaint.resolve", args))
    assert insights.handle(op, verified_args, at, actor=actor)[0] == "ok"
    assert insights.reg.execute("SELECT state FROM complaints")[0]["state"] == "resolved"

"""Control-secret initialization is atomic, repeatable, and never repairs unsafe keys."""
from concurrent.futures import ThreadPoolExecutor
import os
import stat

import pytest

from clinic.control_init import ensure_key
from clinic.insights_access import sign_request, verify_request, ReplayCache


def test_generated_key_is_private_volume_readonly_random_and_silent(tmp_path, capsys):
    key = tmp_path / "editor-rpc.key"
    assert ensure_key(key) == key
    original = key.read_bytes()
    assert len(original) == 32
    assert stat.S_IMODE(key.stat().st_mode) == 0o444
    assert original != bytes(32)
    assert capsys.readouterr().out == ""
    assert ensure_key(key) == key and key.read_bytes() == original
    assert list(tmp_path.glob(".control-*")) == []
    envelope = sign_request("insights.dashboard", {}, {"role": "admin", "username": "admin"}, key_file=key, now=1000)
    assert verify_request(envelope, key_file=key, now=1001, replay_cache=ReplayCache())[0] == "insights.dashboard"


def test_concurrent_initializers_keep_one_complete_key_and_clean_temporaries(tmp_path):
    key = tmp_path / "editor-rpc.key"
    with ThreadPoolExecutor(max_workers=12) as pool:
        paths = list(pool.map(lambda _: ensure_key(key), range(48)))
    assert paths == [key] * 48
    original = key.read_bytes()
    assert len(original) == 32 and stat.S_IMODE(key.stat().st_mode) == 0o444
    assert list(tmp_path.glob(".control-*")) == []
    assert ensure_key(key).read_bytes() == original


@pytest.mark.parametrize("size", [0, 1, 16, 31, 33, 65])
def test_invalid_existing_size_fails_without_overwriting_or_generating(tmp_path, size):
    key = tmp_path / "editor-rpc.key"
    before = b"x" * size
    key.write_bytes(before)
    key.chmod(0o444)
    with pytest.raises(ValueError, match="format or permissions"):
        ensure_key(key)
    assert key.read_bytes() == before
    assert list(tmp_path.glob(".control-*")) == []


@pytest.mark.parametrize("mode", [0o666, 0o620, 0o602])
def test_group_or_world_writable_existing_key_fails_unchanged(tmp_path, mode):
    key = tmp_path / "editor-rpc.key"
    key.write_bytes(os.urandom(32))
    key.chmod(mode)
    before = key.read_bytes()
    with pytest.raises(ValueError, match="permissions"):
        ensure_key(key)
    assert key.read_bytes() == before and stat.S_IMODE(key.stat().st_mode) == mode


@pytest.mark.parametrize("target_exists", [False, True])
def test_symlinks_are_never_followed_or_replaced(tmp_path, target_exists):
    target = tmp_path / "outside.key"
    if target_exists:
        target.write_bytes(os.urandom(32))
        target.chmod(0o444)
    key = tmp_path / "editor-rpc.key"
    key.symlink_to(target)
    with pytest.raises(OSError):
        ensure_key(key)
    assert key.is_symlink()
    assert target.exists() is target_exists
    assert list(tmp_path.glob(".control-*")) == []


def test_named_pipe_is_rejected_without_blocking(tmp_path):
    key = tmp_path / "editor-rpc.key"
    os.mkfifo(key, mode=0o600)
    with pytest.raises((ValueError, OSError)):
        ensure_key(key)
    assert stat.S_ISFIFO(key.stat().st_mode)

"""Authenticated editor -> CRM control envelope for review operations only.

Integration:
    envelope = sign_request(op, args, authenticated_editor_identity)
    # Send only to CRM's internal control endpoint, never to a browser.
    op, args, actor = verify_request(envelope)
    status, payload = insights.handle(op, args, now, closed, actor=actor)

CONTROL_KEY_FILE defaults to /control/editor-rpc.key: exactly 32 random bytes,
shared only by editor and CRM via protected read-only Docker volume mounts.
The init container owns creation. Neither helper creates, logs or returns a key.

Replay prevention is thread-safe within the CRM process. A custom ReplayCache may
be supplied by a service instance; deployments with multiple CRM workers must use
one shared replay store before enabling multiple workers. Timestamp TTL is 30 s.
"""
from collections import OrderedDict
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

ALLOWED_OPS = {"insights.dashboard", "complaint.resolve", "profile.review"}
TTL_SECONDS = 30
MAX_BODY_BYTES = 16 * 1024
MAX_REPLAY_ENTRIES = 4096
DOMAIN = b"clinic-insights-control-v1\x00"
FIELDS = {"version", "op", "args", "actor", "timestamp", "nonce", "signature"}


class AccessError(ValueError):
    """A bounded safe reason; never includes key, envelope or patient content."""


class ReplayCache:
    def __init__(self, max_entries=MAX_REPLAY_ENTRIES):
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("invalid replay cache capacity")
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def consume(self, nonce, timestamp, now):
        with self.lock:
            expired = [key for key, expiry in self.entries.items() if expiry < now]
            for key in expired:
                del self.entries[key]
            if nonce in self.entries:
                raise AccessError("control_request_replayed")
            if len(self.entries) >= self.max_entries:
                # Never evict a live nonce to make replay possible under load.
                raise AccessError("control_replay_capacity_reached")
            self.entries[nonce] = timestamp + TTL_SECONDS


_DEFAULT_REPLAYS = ReplayCache()


def _clock(value):
    result = time.time() if value is None else value
    if type(result) not in {int, float} or not math.isfinite(result) or result < 0:
        raise AccessError("control_clock_invalid")
    return result


def _key(key_file):
    path = Path(key_file or os.environ.get("CONTROL_KEY_FILE", "/control/editor-rpc.key"))
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise AccessError("control_key_permissions_invalid")
            key = stream.read(33)
        if len(key) != 32:
            raise AccessError("control_key_format_invalid")
        return key
    except OSError:
        raise AccessError("control_key_unavailable") from None


def _actor(identity):
    if not isinstance(identity, dict) or identity.get("role") != "admin":
        raise AccessError("control_admin_required")
    username = identity.get("username")
    if not isinstance(username, str) or not re.fullmatch(r"[a-zA-Z0-9_]{3,40}", username):
        raise AccessError("control_identity_invalid")
    return {"role": "admin", "username": username}


def _args(op, args):
    if not isinstance(op, str) or op not in ALLOWED_OPS or not isinstance(args, dict):
        raise AccessError("control_operation_invalid")
    if op == "insights.dashboard":
        if set(args) not in (set(), {"registrar"}):
            raise AccessError("control_arguments_invalid")
        if args:
            number = args["registrar"]
            if not isinstance(number, str) or not re.fullmatch(r"\+?[0-9]{1,24}", number):
                raise AccessError("control_arguments_invalid")
    else:
        expected = {"id", "resolution"} if op == "complaint.resolve" else {"id", "approve"}
        if set(args) != expected or type(args.get("id")) is not int or not 1 <= args["id"] <= 2**63 - 1:
            raise AccessError("control_arguments_invalid")
        if op == "complaint.resolve":
            note = args["resolution"]
            if not isinstance(note, str) or not note.strip() or len(note) > 4000:
                raise AccessError("control_arguments_invalid")
        elif type(args["approve"]) is not bool:
            raise AccessError("control_arguments_invalid")
    return dict(args)


def _canonical(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise AccessError("control_body_invalid") from None
    if len(raw) > MAX_BODY_BYTES:
        raise AccessError("control_body_too_large")
    return raw


def sign_request(op, args, identity, *, key_file=None, now=None, nonce=None):
    """Sign claims derived from an already authenticated editor identity.

    Never pass an identity or actor decoded from browser request JSON here.
    ``nonce``/``now`` exist for deterministic tests; production should omit both.
    """
    actor = _actor(identity)
    safe_args = _args(op, args)
    nonce = secrets.token_hex(16) if nonce is None else nonce
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise AccessError("control_nonce_invalid")
    envelope = {"version": 1, "op": op, "args": safe_args, "actor": actor,
                "timestamp": int(_clock(now)), "nonce": nonce}
    envelope["signature"] = hmac.new(_key(key_file), DOMAIN + _canonical(envelope), hashlib.sha256).hexdigest()
    _canonical(envelope)
    return envelope


def verify_request(envelope, *, key_file=None, now=None, replay_cache=None):
    """Return only verified ``(op, args, actor)``; reject replay before execution."""
    if not isinstance(envelope, dict) or set(envelope) != FIELDS:
        raise AccessError("control_envelope_invalid")
    if type(envelope["version"]) is not int or envelope["version"] != 1:
        raise AccessError("control_version_invalid")
    signature = envelope["signature"]
    nonce = envelope["nonce"]
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise AccessError("control_signature_invalid")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise AccessError("control_nonce_invalid")
    current = _clock(now)
    timestamp = envelope["timestamp"]
    if type(timestamp) is not int or not 0 <= current - timestamp <= TTL_SECONDS:
        raise AccessError("control_request_expired")
    actor = _actor(envelope["actor"])
    if envelope["actor"] != actor:
        raise AccessError("control_identity_invalid")
    op = envelope["op"]
    args = _args(op, envelope["args"])
    unsigned = {"version": 1, "op": op, "args": args, "actor": actor,
                "timestamp": timestamp, "nonce": nonce}
    expected = hmac.new(_key(key_file), DOMAIN + _canonical(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise AccessError("control_signature_invalid")
    _canonical({**unsigned, "signature": signature})
    cache = _DEFAULT_REPLAYS if replay_cache is None else replay_cache
    cache.consume(nonce, timestamp, current)
    return op, args, actor

"""Remote CRM (the hackathon stand) – writes only, ≤ 10 requests per minute.

* :class:`TokenBucket` enforces the stand's rate limit process-wide.
* :class:`Outbox` is a durable-enough FIFO (in-memory + optional JSONL journal)
  processed by one worker thread: the local mirror is updated first (so the
  caller gets an immediate answer), the stand is updated asynchronously, and a
  failed sync raises an operator request instead of silently diverging.
* :class:`HttpRegistry` maps our write operations to the stand's OpenAPI.  The
  exact paths/fields are configured in ``EndpointMap`` – adjust them to the
  spec handed out at the product checkpoint; nothing else needs to change.

Only ``urllib`` is used (no extra dependency, easy to audit).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from calls.services.netutil import open_http
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional


class RateLimited(RuntimeError):
    """The stand answered 429: not an error of ours – wait ``retry_after`` seconds and send the same request again."""

    def __init__(self, retry_after: float, detail: str = "") -> None:
        super().__init__(f"rate limited by the stand, retry after {retry_after:.0f} s {detail}".strip())
        self.retry_after = retry_after


class Unavailable(RuntimeError):
    """5xx / connection error: transient, retried with back-off."""


class TokenBucket:
    """``rate`` requests per ``per`` seconds (default 10/min), thread-safe, blocking."""

    def __init__(self, rate: int = 10, per: float = 60.0, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate, self.per = rate, per
        self._clock, self._sleep = clock, sleep
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait_time(self) -> float:
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= self.per:
            self._stamps.popleft()
        if len(self._stamps) < self.rate:
            return 0.0
        return self.per - (now - self._stamps[0])

    def acquire(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            with self._lock:
                w = self.wait_time()
                if w <= 0:
                    self._stamps.append(self._clock())
                    return True
            if deadline is not None and self._clock() + w > deadline:
                return False
            self._sleep(min(w, 0.5))


#: Default request bodies.  ``{name}`` placeholders are filled from the write payload; a template value that
#: is exactly one placeholder keeps the payload's type (ints stay ints), unresolved placeholders are dropped.
DEFAULT_BODIES: dict[str, dict] = {
    "book": {"patient_id": "{patient_id}", "doctor_id": "{doctor_id}", "slot_id": "{slot_id}", "starts_at": "{starts_at}", "source": "voice-bot", "call_id": "{call_id}"},
    "reschedule": {"doctor_id": "{doctor_id}", "slot_id": "{slot_id}", "starts_at": "{starts_at}", "source": "voice-bot", "call_id": "{call_id}"},
    "confirm": {"confirmed": True, "source": "voice-bot", "call_id": "{call_id}"},
    "operator": {"call_id": "{call_id}", "reason": "{reason}", "patient_id": "{patient_id}", "booking_id": "{remote_id}",
                 "constraints": "{constraints}", "user_state": "{user_state}", "summary": "{summary}"},
}

_KINDS = ("book", "cancel", "reschedule", "confirm", "operator", "list_bookings", "list_patients", "list_doctors")


@dataclass
class EndpointMap:
    """Our five write operations (+ three optional reads) → the stand's OpenAPI.

    Defaults are a guess at a typical appointments API; the real paths/fields come from the organisers' spec and
    are loaded from one JSON file (``STAND_ENDPOINTS``, see ``config/stand.endpoints.json``) – nothing else changes.
    """

    book: tuple[str, str] = ("POST", "/api/v1/appointments")
    cancel: tuple[str, str] = ("DELETE", "/api/v1/appointments/{remote_id}")
    reschedule: tuple[str, str] = ("PATCH", "/api/v1/appointments/{remote_id}")
    confirm: tuple[str, str] = ("POST", "/api/v1/appointments/{remote_id}/confirm")
    operator: tuple[str, str] = ("POST", "/api/v1/operator-requests")
    list_bookings: tuple[str, str] = ("GET", "/api/v1/appointments?date={date}")
    list_patients: tuple[str, str] = ("GET", "/api/v1/patients")
    list_doctors: tuple[str, str] = ("GET", "/api/v1/doctors")
    bodies: dict[str, dict] = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_BODIES.items()})
    id_field: str = "id"            # where the stand returns the created record's id
    items_field: str = "items"      # list responses: ``{"items": [...]}`` or a bare array

    @staticmethod
    def kind_of(kind: str) -> str:
        return "operator" if kind == "transfer" else kind

    def to_dict(self) -> dict:
        out: dict = {k: {"method": getattr(self, k)[0], "path": getattr(self, k)[1]} for k in _KINDS}
        for k, body in self.bodies.items():
            out.setdefault(k, {})["body"] = body
        out["id_field"], out["items_field"] = self.id_field, self.items_field
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "EndpointMap":
        em = cls()
        for k in _KINDS:
            spec = data.get(k)
            if not spec:
                continue
            if isinstance(spec, (list, tuple)):
                method, path = spec
                setattr(em, k, (str(method).upper(), str(path)))
            else:
                setattr(em, k, (str(spec.get("method", getattr(em, k)[0])).upper(), str(spec.get("path", getattr(em, k)[1]))))
                if "body" in spec:
                    if spec["body"] is None:
                        em.bodies.pop(k, None)
                    else:
                        em.bodies[k] = dict(spec["body"])
        em.id_field = str(data.get("id_field", em.id_field))
        em.items_field = str(data.get("items_field", em.items_field))
        return em

    @classmethod
    def load(cls, path: str) -> "EndpointMap":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def render_body(self, kind: str, payload: dict) -> Optional[dict]:
        kind = self.kind_of(kind)
        method = getattr(self, kind)[0]
        if method in ("DELETE", "GET"):
            return None
        template = self.bodies.get(kind)
        if template is None:
            return {k: v for k, v in payload.items() if v is not None}
        return render_template(template, payload)


def render_template(template: dict, payload: dict) -> dict:
    """``{"a": "{x}", "b": "slot {slot_id}", "c": 1}`` + ``{"x": 5, "slot_id": 7}`` → ``{"a": 5, "b": "slot 7", "c": 1}``."""
    out: dict = {}
    for key, tmpl in template.items():
        if isinstance(tmpl, dict):
            sub = render_template(tmpl, payload)
            if sub:
                out[key] = sub
            continue
        if not isinstance(tmpl, str) or "{" not in tmpl:
            out[key] = tmpl
            continue
        stripped = tmpl.strip()
        if stripped.startswith("{") and stripped.endswith("}") and stripped.count("{") == 1:
            name = stripped[1:-1]
            if payload.get(name) is not None:
                out[key] = payload[name]
            continue
        try:
            out[key] = tmpl.format(**{k: ("" if v is None else v) for k, v in payload.items()})
        except (KeyError, IndexError):
            continue
    return out


@dataclass
class HttpRegistry:
    base_url: str
    token: str
    endpoints: EndpointMap = field(default_factory=EndpointMap)
    bucket: TokenBucket = field(default_factory=TokenBucket)
    timeout: float = 10.0
    opener: Callable = open_http  # injectable for tests; http(s)-only by default

    def _request(self, method: str, path: str, body: Optional[dict]) -> dict:
        if not self.bucket.acquire(timeout=120):
            raise TimeoutError("CRM rate limit: no token within 120 s")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url.rstrip("/") + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with self.opener(req, timeout=self.timeout) as resp:  # type: ignore[call-arg]
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200]
            if exc.code == 429:
                try:
                    wait = float(exc.headers.get("Retry-After") or 0) or float(json.loads(detail).get("retry_after", 0)) or 60.0
                except (ValueError, AttributeError, json.JSONDecodeError):
                    wait = 60.0
                raise RateLimited(min(max(wait, 1.0), 120.0), f"({method} {path})") from exc
            if exc.code >= 500:
                raise Unavailable(f"CRM {method} {path} → HTTP {exc.code}: {detail!r}") from exc
            raise RuntimeError(f"CRM {method} {path} → HTTP {exc.code}: {detail!r}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise Unavailable(f"CRM {method} {path} → {exc}") from exc

    def write(self, kind: str, payload: dict) -> dict:
        method, path = getattr(self.endpoints, EndpointMap.kind_of(kind))
        path = path.format(**{k: str(v) for k, v in payload.items()})
        body = self.endpoints.render_body(kind, payload)
        return self._request(method, path, body)

    def read(self, kind: str, **params: object) -> list[dict]:
        """Optional one-off reads (``list_bookings`` for tomorrow's reminders) – they count against the 10 RPM too."""
        method, path = getattr(self.endpoints, kind)
        path = path.format(**{k: str(v) for k, v in params.items()})
        data = self._request(method, path, None)
        if isinstance(data, list):
            return data
        items = data.get(self.endpoints.items_field, data.get("data", []))
        return list(items) if isinstance(items, list) else []


@dataclass
class OutboxItem:
    kind: str
    payload: dict
    attempts: int = 0
    last_error: str = ""
    done: bool = False
    remote: dict = field(default_factory=dict)
    rate_waits: int = 0


class Outbox:
    """FIFO of pending writes processed by a single worker at ≤ bucket rate."""

    def __init__(self, sink: Callable[[str, dict], dict], on_failure: Optional[Callable[[OutboxItem], None]] = None,
                 max_attempts: int = 3, journal_path: Optional[str] = None, sleep: Callable[[float], None] = time.sleep,
                 backoff: tuple[float, ...] = (1.0, 3.0, 10.0), max_rate_waits: int = 10) -> None:
        self.sink, self.on_failure, self.max_attempts = sink, on_failure, max_attempts
        self._sleep, self.backoff, self.max_rate_waits = sleep, backoff, max_rate_waits
        self.rate_waits = 0  # how many times the stand told us to wait (observability)
        self._q: deque[OutboxItem] = deque()
        self._cv = threading.Condition()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self.items: list[OutboxItem] = []
        self._journal = open(journal_path, "a", encoding="utf-8") if journal_path else None

    def put(self, kind: str, payload: dict) -> OutboxItem:
        item = OutboxItem(kind, payload)
        with self._cv:
            self._q.append(item)
            self.items.append(item)
            if self._journal:
                self._journal.write(json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False) + "\n")
                self._journal.flush()
            self._cv.notify()
        return item

    def pending(self) -> int:
        with self._cv:
            return len(self._q)

    def start(self) -> "Outbox":
        self._thread = threading.Thread(target=self._run, name="crm-outbox", daemon=True)
        self._thread.start()
        return self

    def stop(self, drain: bool = True, timeout: float = 5.0) -> None:
        if drain:
            self.join(timeout)
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout)

    def join(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._q and time.monotonic() < deadline:
                self._cv.wait(0.05)
            return not self._q

    def process_one(self) -> Optional[OutboxItem]:
        with self._cv:
            if not self._q:
                return None
            item = self._q[0]
        try:
            item.remote = self.sink(item.kind, item.payload)
            item.done = True
        except RateLimited as exc:  # 429 → wait as told, same item stays at the head of the queue; not counted as an attempt
            item.rate_waits += 1
            self.rate_waits += 1
            item.last_error = str(exc)
            if item.rate_waits <= self.max_rate_waits:
                self._sleep(exc.retry_after)
                return item
            item.attempts = self.max_attempts
            if self.on_failure:
                self.on_failure(item)
        except Exception as exc:  # noqa: BLE001 - any transport error
            item.attempts += 1
            item.last_error = str(exc)
            if item.attempts < self.max_attempts:
                self._sleep(self.backoff[min(item.attempts - 1, len(self.backoff) - 1)])
                return item
            if self.on_failure:
                self.on_failure(item)
        with self._cv:
            if self._q and self._q[0] is item:
                self._q.popleft()
            self._cv.notify_all()
        return item

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._q and not self._stop:
                    self._cv.wait(0.2)
                if self._stop and not self._q:
                    return
            self.process_one()

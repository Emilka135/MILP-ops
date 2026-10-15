"""Module bus: one bounded priority queue + worker pool per module, correlation ids, deadlines.

Design goals (from the brief): modules run in parallel, each has its own queue
with measured waiting/processing time, a response can never be attributed to
the wrong request, and work that cannot meet its deadline is dropped early
instead of clogging the queue.

* ``Request.id`` is unique per process; the ``Response`` carries it back and the
  ``Future`` is resolved only by the worker that executed exactly that request.
* Queues are earliest-deadline-first (``PriorityQueue`` keyed by deadline), so
  an interactive call beats a batch transcription.
* ``submit`` raises :class:`Overloaded` when the bounded queue is full – the
  orchestrator then degrades (e.g. says "подождите" or hands over to a human)
  instead of letting latency grow without bound.
* Per-module metrics: depth, processed, expired, errors, queue/service time
  percentiles – exported by ``/metrics`` in the HTTP services.
"""

from __future__ import annotations

import itertools
import queue
import statistics
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Optional

Handler = Callable[[str, dict], tuple[str, dict]]


class Overloaded(RuntimeError):
    pass


@dataclass(order=True)
class Request:
    deadline: float
    seq: int
    id: int = field(compare=False)
    module: str = field(compare=False)
    op: str = field(compare=False)
    payload: dict = field(compare=False, default_factory=dict)
    call_id: str = field(compare=False, default="")
    enqueued_at: float = field(compare=False, default=0.0)
    future: "Future[Response]" = field(compare=False, default_factory=Future)


@dataclass
class Response:
    request_id: int
    module: str
    op: str
    status: str
    payload: dict
    call_id: str
    queued_ms: float
    service_ms: float
    worker: str

    @property
    def ok(self) -> bool:
        return self.status not in ("error", "expired")


@dataclass
class ModuleSpec:
    name: str
    handler: Handler
    workers: int = 2
    maxsize: int = 256
    default_timeout: float = 5.0


class _Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.processed = 0
        self.expired = 0
        self.errors = 0
        self.rejected = 0
        self.queued: list[float] = []
        self.service: list[float] = []

    def add(self, queued_ms: float, service_ms: float, status: str) -> None:
        with self.lock:
            self.processed += 1
            if status == "expired":
                self.expired += 1
            elif status == "error":
                self.errors += 1
            self.queued.append(queued_ms)
            self.service.append(service_ms)
            if len(self.queued) > 5000:
                del self.queued[:2500]
                del self.service[:2500]

    def snapshot(self) -> dict:
        with self.lock:
            def pct(xs: list[float], p: float) -> float:
                if not xs:
                    return 0.0
                return round(statistics.quantiles(xs, n=100)[int(p) - 1] if len(xs) > 1 else xs[0], 2)

            return {
                "processed": self.processed, "expired": self.expired, "errors": self.errors, "rejected": self.rejected,
                "queued_ms_p50": pct(self.queued, 50), "queued_ms_p95": pct(self.queued, 95),
                "service_ms_p50": pct(self.service, 50), "service_ms_p95": pct(self.service, 95),
            }


class Bus:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._modules: dict[str, ModuleSpec] = {}
        self._queues: dict[str, "queue.PriorityQueue[Request]"] = {}
        self._threads: list[threading.Thread] = []
        self._stats: dict[str, _Stats] = {}
        self._seq = itertools.count()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._running = False

    # ---- lifecycle ----------------------------------------------------------------------

    def register(self, spec: ModuleSpec) -> None:
        if spec.name in self._modules:
            raise ValueError(f"module {spec.name!r} already registered")
        self._modules[spec.name] = spec
        self._queues[spec.name] = queue.PriorityQueue(maxsize=spec.maxsize)
        self._stats[spec.name] = _Stats()
        if self._running:
            self._spawn(spec)

    def start(self) -> "Bus":
        self._running = True
        for spec in self._modules.values():
            self._spawn(spec)
        return self

    def _spawn(self, spec: ModuleSpec) -> None:
        for i in range(spec.workers):
            t = threading.Thread(target=self._worker, args=(spec, f"{spec.name}-{i}"), name=f"bus-{spec.name}-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._running = False
        for spec in self._modules.values():
            for _ in range(spec.workers):  # sentinel sorts last → queued work is drained first
                self._queues[spec.name].put(Request(float("inf"), next(self._seq), -1, spec.name, "__stop__"))
        for t in self._threads:
            t.join(timeout)
        self._threads.clear()

    def __enter__(self) -> "Bus":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- submit -------------------------------------------------------------------------------

    def submit(self, module: str, op: str, payload: dict, call_id: str = "", timeout: Optional[float] = None) -> "Future[Response]":
        spec = self._modules[module]
        now = self._clock()
        req = Request(
            deadline=now + (timeout if timeout is not None else spec.default_timeout), seq=next(self._seq), id=next(self._ids),
            module=module, op=op, payload=payload, call_id=call_id, enqueued_at=now,
        )
        try:
            self._queues[module].put_nowait(req)
        except queue.Full:
            self._stats[module].rejected += 1
            raise Overloaded(f"module {module!r} queue full ({spec.maxsize})") from None
        return req.future

    def call(self, module: str, op: str, payload: dict, call_id: str = "", timeout: Optional[float] = None) -> Response:
        """Blocking convenience wrapper; returns an ``expired`` Response instead of raising on timeout."""
        fut = self.submit(module, op, payload, call_id, timeout)
        spec = self._modules[module]
        try:
            return fut.result(timeout=(timeout if timeout is not None else spec.default_timeout) + 0.5)
        except TimeoutError:
            return Response(-1, module, op, "expired", {"error": "caller timeout"}, call_id, 0.0, 0.0, "")

    # ---- workers ------------------------------------------------------------------------------

    def _worker(self, spec: ModuleSpec, name: str) -> None:
        q = self._queues[spec.name]
        stats = self._stats[spec.name]
        while True:
            req = q.get()
            if req.op == "__stop__":
                return
            started = self._clock()
            queued_ms = (started - req.enqueued_at) * 1000
            if started > req.deadline:
                resp = Response(req.id, req.module, req.op, "expired", {"error": "deadline passed in queue"}, req.call_id, queued_ms, 0.0, name)
                stats.add(queued_ms, 0.0, "expired")
                req.future.set_result(resp)
                continue
            try:
                status, payload = spec.handler(req.op, req.payload)
            except Exception as exc:  # noqa: BLE001 – a module bug must not kill the worker
                status, payload = "error", {"error": f"{type(exc).__name__}: {exc}"}
            service_ms = (self._clock() - started) * 1000
            stats.add(queued_ms, service_ms, status)
            req.future.set_result(Response(req.id, req.module, req.op, status, payload, req.call_id, queued_ms, service_ms, name))

    # ---- observability -----------------------------------------------------------------------

    def metrics(self) -> dict[str, dict]:
        out = {}
        for name, st in self._stats.items():
            snap = st.snapshot()
            snap["depth"] = self._queues[name].qsize()
            snap["workers"] = self._modules[name].workers
            out[name] = snap
        return out

    def modules(self) -> list[str]:
        return list(self._modules)

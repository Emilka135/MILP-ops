import random
import threading
import time
from concurrent.futures import wait

import pytest

from calls.orchestrator.bus import Bus, ModuleSpec, Overloaded


def echo_handler(op, args):
    time.sleep(args.get("sleep", 0))
    if op == "boom":
        raise RuntimeError("module bug")
    return "ok", {"echo": args["n"], "op": op}


def test_no_request_response_mixups_under_concurrency():
    bus = Bus()
    for name, workers in (("a", 3), ("b", 1), ("c", 4)):
        bus.register(ModuleSpec(name, echo_handler, workers=workers, maxsize=5000))
    with bus:
        rnd = random.Random(1)
        futures = []
        lock = threading.Lock()

        def producer(tid):
            for i in range(300):
                n = tid * 1000 + i
                mod = rnd.choice("abc")
                fut = bus.submit(mod, "echo", {"n": n, "sleep": rnd.choice([0, 0, 0.0005])}, call_id=f"call-{tid}")
                with lock:
                    futures.append((fut, n, mod, f"call-{tid}"))

        threads = [threading.Thread(target=producer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wait([f for f, *_ in futures], timeout=30)
        for fut, n, mod, call_id in futures:
            r = fut.result(timeout=0)
            assert r.status == "ok" and r.payload["echo"] == n and r.module == mod and r.call_id == call_id
        ids = [f.result().request_id for f, *_ in futures]
        assert len(set(ids)) == len(ids)
        m = bus.metrics()
        assert sum(v["processed"] for v in m.values()) == 1200 and all(v["errors"] == 0 for v in m.values())
        assert m["a"]["workers"] == 3 and m["a"]["service_ms_p95"] >= 0


def test_deadline_expired_work_is_dropped_not_executed():
    calls = []

    def slow(op, args):
        calls.append(args["n"])
        time.sleep(0.05)
        return "ok", {}

    bus = Bus()
    bus.register(ModuleSpec("slow", slow, workers=1, default_timeout=0.02))
    with bus:
        futs = [bus.submit("slow", "x", {"n": i}) for i in range(5)]
        results = [f.result(timeout=5) for f in futs]
    statuses = [r.status for r in results]
    assert statuses[0] == "ok" and "expired" in statuses
    assert len(calls) < 5  # the expired ones never ran
    assert bus.metrics()["slow"]["expired"] == statuses.count("expired")


def test_overload_raises_instead_of_queueing_forever():
    bus = Bus()
    bus.register(ModuleSpec("tiny", lambda op, a: (time.sleep(0.05), ("ok", {}))[1], workers=1, maxsize=2))
    with bus:
        bus.submit("tiny", "x", {})
        time.sleep(0.01)  # worker picks it up
        bus.submit("tiny", "x", {})
        bus.submit("tiny", "x", {})  # one running + two queued
        with pytest.raises(Overloaded):
            bus.submit("tiny", "x", {})
        time.sleep(0.3)
    assert bus.metrics()["tiny"]["rejected"] == 1


def test_handler_exception_becomes_error_response_and_worker_survives():
    bus = Bus()
    bus.register(ModuleSpec("e", echo_handler, workers=1))
    with bus:
        r1 = bus.call("e", "boom", {"n": 1})
        r2 = bus.call("e", "echo", {"n": 2})
    assert r1.status == "error" and "module bug" in r1.payload["error"]
    assert r2.status == "ok" and r2.payload["echo"] == 2


def test_earliest_deadline_first():
    order = []

    def h(op, args):
        order.append(args["n"])
        time.sleep(0.02)
        return "ok", {}

    bus = Bus()
    bus.register(ModuleSpec("p", h, workers=1, maxsize=100, default_timeout=10))
    with bus:
        bus.submit("p", "x", {"n": "first"})  # occupies the worker
        time.sleep(0.005)
        f_late = bus.submit("p", "x", {"n": "late"}, timeout=5.0)
        f_urgent = bus.submit("p", "x", {"n": "urgent"}, timeout=0.5)
        wait([f_late, f_urgent], timeout=5)
    assert order == ["first", "urgent", "late"]

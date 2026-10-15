"""The stand (organisers' CRM) – the solution's **input / output contract** in one place.

INPUT – read once into the local mirror (the stand allows 10 requests per minute, so we never query it per call):

* doctors' schedule – the Excel/CSV handed out by the organisers (``CRM_SCHEDULE``)
* patients – CSV/XLSX export (``CRM_PATIENTS``) or demo data (``CRM_SEED_DEMO=1``)
* bookings that already exist in the stand, with the stand's ids (``CRM_BOOKINGS`` CSV/JSON/XLSX or
  :meth:`StandSync.pull_bookings` = one GET per day)

OUTPUT – every state change the bot makes goes to the stand through :class:`calls.crm.remote.Outbox`:

====================  ==========  =======================================================
mirror event          stand kind  default request (configurable via ``STAND_ENDPOINTS``)
====================  ==========  =======================================================
booking created       book        ``POST   /api/v1/appointments``
booking moved         reschedule  ``PATCH  /api/v1/appointments/{remote_id}``
booking cancelled     cancel      ``DELETE /api/v1/appointments/{remote_id}``
booking confirmed     confirm     ``POST   /api/v1/appointments/{remote_id}/confirm``
transfer to operator  operator    ``POST   /api/v1/operator-requests``
====================  ==========  =======================================================

Ordering is FIFO per process, so ``cancel`` of a booking the bot itself created always follows its ``book``
and the stand id (``remote_id``) is known by then.  A write that still fails after ``max_attempts`` becomes an
operator request (``reason = stand_sync_failed``): the registrar fixes it by hand, nothing diverges silently.

Modes (``STAND_MODE``): ``live`` (default when ``STAND_URL`` is set), ``dry`` (journal only – demo without the
stand), ``off``.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from typing import Callable, Mapping, Optional

from calls.crm.registry import MockRegistry
from calls.crm.remote import EndpointMap, HttpRegistry, Outbox, OutboxItem, TokenBucket


class StandError(RuntimeError):
    """A write cannot be mapped onto the stand (e.g. the booking has no remote id)."""


class StandSync:
    """Glue: ``CrmModule.on_write`` / ``OperatorModule.on_ticket`` → Outbox → stand → mirror bookkeeping."""

    KINDS = ("book", "cancel", "reschedule", "confirm", "transfer", "operator")

    def __init__(self, reg: MockRegistry, client: Optional[HttpRegistry], *, mode: str = "live", journal_path: Optional[str] = None,
                 max_attempts: int = 3, on_failure: Optional[Callable[[OutboxItem], None]] = None) -> None:
        if mode not in ("live", "dry"):
            raise ValueError(f"STAND_MODE must be live|dry|off, got {mode!r}")
        if mode == "live" and client is None:
            raise ValueError("live mode needs an HttpRegistry client")
        self.reg, self.client, self.mode = reg, client, mode
        self._on_failure = on_failure
        self.outbox = Outbox(self._sink, on_failure=self._failed, max_attempts=max_attempts, journal_path=journal_path)
        self.done: list[dict] = []
        self.failed: list[OutboxItem] = []
        self._lock = threading.Lock()

    # ---- construction -------------------------------------------------------------------------

    @classmethod
    def from_env(cls, reg: MockRegistry, env: Optional[Mapping[str, str]] = None) -> Optional["StandSync"]:
        """``None`` when the stand is not configured (``STAND_URL`` empty and ``STAND_MODE`` not ``dry``)."""
        env = os.environ if env is None else env
        url = (env.get("STAND_URL") or "").strip()
        mode = (env.get("STAND_MODE") or ("live" if url else "off")).strip().lower()
        if mode == "off":
            return None
        if mode == "live" and not url:
            raise ValueError("STAND_MODE=live requires STAND_URL")
        endpoints = EndpointMap.load(env["STAND_ENDPOINTS"]) if env.get("STAND_ENDPOINTS") else EndpointMap()
        client = None
        if mode == "live":
            bucket = TokenBucket(int(env.get("STAND_RPM", "10") or 10), 60.0)
            client = HttpRegistry(url, env.get("STAND_TOKEN", "") or "", endpoints, bucket, timeout=float(env.get("STAND_TIMEOUT", "10") or 10))
        sync = cls(reg, client, mode=mode, journal_path=env.get("STAND_JOURNAL") or None, max_attempts=int(env.get("STAND_ATTEMPTS", "3") or 3))
        return sync.start()

    def start(self) -> "StandSync":
        self.outbox.start()
        return self

    def stop(self, drain: bool = True, timeout: float = 5.0) -> None:
        self.outbox.stop(drain=drain, timeout=timeout)

    def join(self, timeout: float = 30.0) -> bool:
        return self.outbox.join(timeout)

    # ---- the hook the modules call --------------------------------------------------------------

    def on_write(self, kind: str, payload: dict) -> None:
        if kind not in self.KINDS:
            raise StandError(f"unknown write kind {kind!r}")
        self.outbox.put(kind, dict(payload))

    # ---- outbox sink: one write to the stand + bookkeeping ---------------------------------------

    def _resolve(self, kind: str, payload: dict) -> dict:
        p = dict(payload)
        k = EndpointMap.kind_of(kind)
        if k in ("cancel", "confirm", "reschedule"):
            bid = p.get("old_booking_id") if k == "reschedule" else p.get("booking_id")
            rid = self.reg.remote_id_of(int(bid)) if bid is not None else None
            if rid is None:
                raise StandError(f"{k}: booking {bid} has no stand id (was never created in the stand)")
            p["remote_id"] = rid
        elif k == "operator" and p.get("booking_id") is not None and not p.get("remote_id"):
            p["remote_id"] = self.reg.remote_id_of(int(p["booking_id"]))
        return p

    def _sink(self, kind: str, payload: dict) -> dict:
        p = self._resolve(kind, payload)
        k = EndpointMap.kind_of(kind)
        if self.mode == "dry" or self.client is None:
            resp: dict = {"dry_run": True, "id": f"dry-{k}-{p.get('booking_id', p.get('request_id', ''))}"}
        else:
            resp = self.client.write(kind, p)
        id_field = self.client.endpoints.id_field if self.client else "id"
        new_id = resp.get(id_field) if isinstance(resp, dict) else None
        if k == "book":
            self.reg.mark_booking_synced(p["booking_id"], None if new_id is None else str(new_id))
        elif k == "reschedule":
            self.reg.mark_booking_synced(p["booking_id"], str(new_id) if new_id is not None else p["remote_id"])
            self.reg.mark_booking_synced(p["old_booking_id"])
        elif k in ("cancel", "confirm"):
            self.reg.mark_booking_synced(p["booking_id"])
        elif k == "operator" and p.get("request_id") is not None:
            self.reg.mark_operator_synced(int(p["request_id"]), None if new_id is None else str(new_id))
        with self._lock:
            self.done.append({"kind": kind, "payload": p, "response": resp, "at": datetime.now().isoformat(timespec="seconds")})
        return resp

    def _failed(self, item: OutboxItem) -> None:
        k = EndpointMap.kind_of(item.kind)
        p = item.payload
        try:
            if k in ("book", "reschedule", "cancel", "confirm") and p.get("booking_id") is not None:
                self.reg.mark_booking_synced(p["booking_id"], state="failed")
            elif k == "operator" and p.get("request_id") is not None:
                self.reg.mark_operator_synced(int(p["request_id"]), state="failed")
            # never diverge silently: a human finishes what the API could not
            self.reg.record_operator_request(str(p.get("call_id") or ""), "stand_sync_failed",
                                             json.dumps({"kind": item.kind, "payload": p, "error": item.last_error, "attempts": item.attempts}, ensure_ascii=False, default=str))
        finally:
            with self._lock:
                self.failed.append(item)
            if self._on_failure:
                self._on_failure(item)

    # ---- observability --------------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            done, failed = len(self.done), len(self.failed)
            last = self.done[-1] if self.done else None
        return {
            "mode": self.mode,
            "base_url": self.client.base_url if self.client else None,
            "rpm": self.client.bucket.rate if self.client else None,
            "pending": self.outbox.pending(),
            "done": done,
            "failed": failed,
            "rate_waits": self.outbox.rate_waits,
            "last": last,
            "mirror": self.reg.sync_report(),
        }

    def writes(self) -> list[dict]:
        with self._lock:
            return list(self.done)

    # ---- optional input: existing bookings from the stand -----------------------------------------

    def pull_bookings(self, day: date) -> int:
        """One GET (counts against the 10 RPM) → mirror rows with the stand's ids; used before the reminder campaign."""
        if self.client is None:
            return 0
        items = self.client.read("list_bookings", date=day.isoformat())
        rows = []
        for it in items:
            rows.append({
                "remote_id": it.get("id") or it.get("remote_id") or it.get("appointment_id"),
                "patient_id": it.get("patient_id") or (it.get("patient") or {}).get("id"),
                "doctor": it.get("doctor_id") or it.get("doctor") or (it.get("doctor") or {}).get("id") or "",
                "starts_at": it.get("starts_at") or it.get("datetime") or it.get("start") or f"{it.get('date')}T{it.get('time')}",
                "confirmed": it.get("confirmed", False),
            })
        return self.reg.load_bookings_rows(rows)


def load_inputs_from_env(reg: MockRegistry, env: Optional[Mapping[str, str]] = None, log: Callable[[str], None] = print) -> dict:
    """``CRM_SCHEDULE`` / ``CRM_PATIENTS`` / ``CRM_BOOKINGS`` → mirror.  Returns the counts (``{}`` when nothing configured)."""
    env = os.environ if env is None else env
    out: dict = {}
    sched = env.get("CRM_SCHEDULE")
    if sched:
        out["slots"] = reg.load_schedule_xlsx(sched) if sched.lower().endswith((".xlsx", ".xlsm")) else reg.load_schedule_csv(sched)
        log(f"schedule: {out['slots']} slots from {sched}")
    pats = env.get("CRM_PATIENTS")
    if pats:
        out["patients"] = reg.load_patients_xlsx(pats) if pats.lower().endswith((".xlsx", ".xlsm")) else reg.load_patients_csv(pats)
        log(f"patients: {out['patients']} rows from {pats}")
    books = env.get("CRM_BOOKINGS")
    if books:
        low = books.lower()
        if low.endswith(".json"):
            out["bookings"] = reg.load_bookings_json(books)
        elif low.endswith((".xlsx", ".xlsm")):
            out["bookings"] = reg.load_bookings_xlsx(books)
        else:
            out["bookings"] = reg.load_bookings_csv(books)
        log(f"bookings: {out['bookings']} rows from {books}")
    return out

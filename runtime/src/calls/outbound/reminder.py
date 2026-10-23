"""Builds the next-day reminder campaign from the local CRM mirror and drives it under the policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from calls.crm.module import specialty_dative
from calls.crm.registry import MockRegistry
from calls.tts.verbalize import date_to_words, time_to_words

from .policy import CampaignItem, Decision, DialPolicy, decide, record_attempt


def build_campaign(reg: MockRegistry, for_day: date, clinic: dict, today: Optional[date] = None) -> list[CampaignItem]:
    """One item per *unconfirmed* booking on ``for_day`` whose patient has a phone."""
    items: list[CampaignItem] = []
    for b in reg.bookings_on(for_day):
        if b.confirmed:
            continue
        p = reg.patient_by_id(b.patient_id)
        if p is None or not p.phone:
            continue
        vars = {
            "patient_id": p.id, "patient_name": p.short_name, "booking_id": b.id, "specialty": b.specialty,
            "specialty_dative": specialty_dative(b.specialty), "doctor": b.doctor, "cabinet": b.cabinet,
            "slot_id": b.slot_id, "slot_date": b.starts_at.date().isoformat(), "slot_time": b.starts_at.strftime("%H:%M"),
            "slot_date_spoken": date_to_words(b.starts_at.date()), "slot_time_spoken": time_to_words(b.starts_at.time()),
            "today": (today or date.today()).isoformat(), **clinic,
        }
        items.append(CampaignItem(b.id, p.phone, b.starts_at, vars))
    return items


@dataclass
class Campaign:
    items: list[CampaignItem]
    policy: DialPolicy = field(default_factory=DialPolicy)
    log: list[tuple[datetime, int, str]] = field(default_factory=list)

    def due(self, now: datetime) -> list[tuple[CampaignItem, Decision]]:
        out = []
        for it in self.items:
            d = decide(it, now, self.policy)
            if d.allowed:
                out.append((it, d))
        return out

    def run_tick(self, now: datetime, dial: Callable[[CampaignItem], str], max_parallel: int = 4) -> int:
        """Dial everything that is due (bounded), record outcomes.  ``dial`` returns the outcome."""
        n = 0
        for item, d in self.due(now)[:max_parallel]:
            outcome = dial(item)
            record_attempt(item, now, outcome, self.policy)
            self.log.append((now, item.booking_id, f"{d.reason} → {outcome}"))
            n += 1
        return n

    def next_wakeup(self, now: datetime) -> Optional[datetime]:
        times = [decide(it, now, self.policy).not_before for it in self.items if not it.closed]
        times = [t for t in times if t is not None]
        return min(times) if times else (now + timedelta(minutes=5) if any(not it.closed for it in self.items) else None)


# ---- CLI -------------------------------------------------------------------------------------------------


def _dial_via_orchestrator(url: str, timeout: float) -> Callable[[CampaignItem], str]:
    """Start a ``reminder`` call through the orchestrator service and map the final status to a policy outcome.

    The telephony bridge (telephony/ari_bridge.py) originates the actual SIP call when it sees the new session;
    here we only poll ``call.state`` until the session finishes or ``timeout`` elapses.
    """
    import time as _time
    import uuid

    from calls.services.netutil import post_json

    def post(op: str, args: dict) -> dict:
        return post_json(f"{url.rstrip('/')}/v1/{op}", args, timeout=10)

    def dial(item: CampaignItem) -> str:
        call_id = f"rem-{item.booking_id}-{uuid.uuid4().hex[:8]}"
        res = post("call.start", {"call_id": call_id, "flow": "reminder", "caller_phone": item.phone, "vars": item.vars, "direction": "outbound"})
        if res.get("status") != "ok":
            return "failed"
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            st = post("call.state", {"call_id": call_id}).get("payload", {})
            if st.get("finished"):
                status = st.get("status") or ""
                if status in ("confirmed", "cancelled", "rescheduled", "transfer", "transferred"):
                    return "answered"
                if status == "no_answer":
                    return "no_answer"
                return "answered" if st.get("answered") else "no_answer"
            _time.sleep(1.0)
        post("call.hangup", {"call_id": call_id})
        return "no_answer"

    return dial


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import os
    import time as _time

    from calls.crm.registry import seed_demo

    ap = argparse.ArgumentParser(description="Next-day reminder campaign (10:00–22:00, ≤6 calls/24 h per patient)")
    ap.add_argument("--db", default=os.environ.get("CRM_DB", ":memory:"))
    ap.add_argument("--for-day", type=date.fromisoformat, default=None, help="default: tomorrow")
    ap.add_argument("--orchestrator", default=os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8080"))
    ap.add_argument("--call-timeout", type=float, default=float(os.environ.get("REMINDER_CALL_TIMEOUT", "180")))
    ap.add_argument("--loop", action="store_true", help="keep running until every item is closed")
    ap.add_argument("--dry-run", action="store_true", help="print the campaign and the first decisions, do not dial")
    ap.add_argument("--seed-demo", action="store_true", default=os.environ.get("CRM_SEED_DEMO", "0") == "1")
    ns = ap.parse_args(argv)

    reg = MockRegistry(ns.db)
    today = date.today()
    if ns.seed_demo:
        seed_demo(reg, today)
    for_day = ns.for_day or (today + timedelta(days=1))
    clinic = {"clinic_name": os.environ.get("CLINIC_NAME", "поликлиники"), "clinic_phone": os.environ.get("CLINIC_PHONE", "+7 000 000-00-00")}
    items = build_campaign(reg, for_day, clinic, today)
    camp = Campaign(items)
    print(f"campaign for {for_day}: {len(items)} unconfirmed bookings", flush=True)
    if ns.dry_run:
        now = datetime.now()
        for it in items:
            d = decide(it, now, camp.policy)
            print(f"  booking {it.booking_id} → {it.phone[-4:].rjust(len(it.phone), '*')} {it.vars['slot_time']} : {'dial now' if d.allowed else f'wait until {d.not_before}'} ({d.reason})")
        return 0
    dial = _dial_via_orchestrator(ns.orchestrator, ns.call_timeout)
    while True:
        now = datetime.now()
        n = camp.run_tick(now, dial)
        for ts, bid, msg in camp.log[-n:] if n else []:
            print(f"{ts:%H:%M:%S} booking {bid}: {msg}", flush=True)
        nxt = camp.next_wakeup(datetime.now())
        if nxt is None or not ns.loop:
            break
        _time.sleep(max(1.0, min(300.0, (nxt - datetime.now()).total_seconds())))
    closed = sum(1 for it in items if it.closed)
    print(f"done: {closed}/{len(items)} items closed", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())

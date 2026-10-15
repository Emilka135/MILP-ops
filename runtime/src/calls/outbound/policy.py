"""Outbound dialing policy: time window + attempt budget (no pestering, no night calls).

Rules (configurable, defaults from the brief):

* calls only between 10:00 and 22:00 **local time of the patient / clinic**;
* at most ``max_calls_per_day`` = 6 dials to one number per rolling 24 h,
  organised as ``occasions_per_day`` = 3 occasions × ``attempts_per_occasion`` = 2
  attempts (2nd attempt ``retry_gap`` minutes after a no-answer);
* occasions are spread over the window (e.g. 10:00–14:00, 14:00–18:00, 18:00–22:00);
* a successful contact (any answer that reached the scenario) closes the campaign item;
* the reminder is for appointments ``days_ahead`` = 1 day ahead (next-day reminders).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional


@dataclass(frozen=True)
class DialPolicy:
    window_start: time = time(10, 0)
    window_end: time = time(22, 0)
    max_calls_per_day: int = 6
    occasions_per_day: int = 3
    attempts_per_occasion: int = 2
    retry_gap: timedelta = timedelta(minutes=20)
    min_gap_between_occasions: timedelta = timedelta(hours=3)
    days_ahead: int = 1

    def in_window(self, at: datetime) -> bool:
        t = at.time()
        return self.window_start <= t < self.window_end

    def next_window_open(self, at: datetime) -> datetime:
        if self.in_window(at):
            return at
        if at.time() < self.window_start:
            return datetime.combine(at.date(), self.window_start)
        return datetime.combine(at.date() + timedelta(days=1), self.window_start)


@dataclass
class Attempt:
    at: datetime
    outcome: str  # answered | no_answer | busy | failed | voicemail


@dataclass
class CampaignItem:
    booking_id: int
    phone: str
    appointment_at: datetime
    vars: dict
    attempts: list[Attempt] = field(default_factory=list)
    closed: bool = False
    close_reason: str = ""

    def dials_last_24h(self, now: datetime) -> int:
        return sum(1 for a in self.attempts if now - a.at < timedelta(hours=24))

    def occasions(self) -> list[list[Attempt]]:
        """Group attempts into occasions: attempts closer than 1 h belong to the same occasion."""
        groups: list[list[Attempt]] = []
        for a in sorted(self.attempts, key=lambda x: x.at):
            if groups and a.at - groups[-1][-1].at <= timedelta(hours=1):
                groups[-1].append(a)
            else:
                groups.append([a])
        return groups


@dataclass
class Decision:
    allowed: bool
    reason: str
    not_before: Optional[datetime] = None


def decide(item: CampaignItem, now: datetime, policy: DialPolicy = DialPolicy()) -> Decision:
    """May we dial ``item`` right now?  If not, when at the earliest?"""
    if item.closed:
        return Decision(False, f"closed: {item.close_reason}")
    if item.appointment_at <= now:
        return Decision(False, "appointment already started")
    if not policy.in_window(now):
        return Decision(False, "outside 10:00–22:00", policy.next_window_open(now))
    if item.dials_last_24h(now) >= policy.max_calls_per_day:
        oldest = min(a.at for a in item.attempts if now - a.at < timedelta(hours=24))
        return Decision(False, "daily budget exhausted", policy.next_window_open(oldest + timedelta(hours=24)))
    occasions = item.occasions()
    if occasions:
        last = occasions[-1]
        if len(last) < policy.attempts_per_occasion:
            gap = now - last[-1].at
            if gap < policy.retry_gap:
                return Decision(False, "retry gap", last[-1].at + policy.retry_gap)
            return Decision(True, f"attempt {len(last) + 1} of occasion {len(occasions)}")
        today_occasions = [g for g in occasions if now - g[0].at < timedelta(hours=24)]
        if len(today_occasions) >= policy.occasions_per_day:
            return Decision(False, "occasions exhausted", policy.next_window_open(today_occasions[0][0].at + timedelta(hours=24)))
        if now - last[-1].at < policy.min_gap_between_occasions:
            return Decision(False, "occasion gap", policy.next_window_open(last[-1].at + policy.min_gap_between_occasions))
        return Decision(True, f"occasion {len(occasions) + 1}")
    return Decision(True, "first attempt")


def record_attempt(item: CampaignItem, at: datetime, outcome: str, policy: DialPolicy = DialPolicy()) -> None:
    item.attempts.append(Attempt(at, outcome))
    if outcome == "answered":
        item.closed, item.close_reason = True, "answered"
    elif len(item.attempts) >= policy.max_calls_per_day:
        item.closed, item.close_reason = True, "budget exhausted"


def reminder_day(appointment: date, policy: DialPolicy = DialPolicy()) -> date:
    return appointment - timedelta(days=policy.days_ahead)

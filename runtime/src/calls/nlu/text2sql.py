"""Deterministic text→SQL automaton (experimental second NLU path).

Instead of *intent → scenario state → CRM call*, the utterance is scanned once by
a finite automaton over token classes and compiled straight into a parameterised
SQL statement against the local CRM mirror (``calls.crm.registry`` schema).

    "запишите меня к кардиологу на завтра после обеда"
        → find_slots: SELECT … WHERE d.specialty = :specialty AND date(s.starts_at) = :day
                              AND time(s.starts_at) >= :t_from AND time(s.starts_at) < :t_to

Everything is table driven: ``LEXICON`` maps word stems to token classes,
``TRANSITIONS`` is the (state, class) → state table, ``emit`` renders SQL from
the slots collected along the accepted path.  Unknown words are ignored
(self-loops), conflicting intents send the automaton to ``reject`` → operator.
``tools/bench_nlu.py`` compares its latency with the rules→scenario→SQL path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Optional

from .intent import SPECIALTIES, extract_surname
from .normalize import Interpretation, interpret

DAYPART_WINDOWS: dict[str, tuple[str, str]] = {
    "morning": ("08:00", "12:00"),
    "midday": ("12:00", "15:00"),
    "afternoon": ("13:00", "17:00"),
    "evening": ("17:00", "20:00"),
}

# token classes ---------------------------------------------------------------------
LEXICON: dict[str, str] = {}
for _stem in ("запис", "запиш", "талон", "попасть", "прием"):
    LEXICON[_stem] = "BOOK"
for _stem in ("перенес", "перенос", "перезапис", "поменя", "измени", "сдвин", "передвин"):
    LEXICON[_stem] = "RESCHEDULE"
for _stem in ("отмен", "откаж", "отказ", "аннул", "снят", "сними", "уберит", "убрат"):
    LEXICON[_stem] = "CANCEL"
for _stem in ("подтвер", "приду", "в силе"):
    LEXICON[_stem] = "CONFIRM"
for _stem in ("когда у меня", "во сколько у меня", "моя запись", "мою запись", "к кому я", "записан", "проверь", "уточни"):
    LEXICON[_stem] = "MYBOOKING"
for _stem in ("расписан", "график", "часы работы", "режим работы", "до скольки", "со скольки", "работает"):
    LEXICON[_stem] = "SCHEDULE"
for _stem in ("праздни", "выходн", "новогодн", "нерабоч"):
    LEXICON[_stem] = "HOLIDAYS"
for _stem in ("адрес", "как добраться", "как проехать", "как пройти", "где находит", "где вы находитесь", "документ", "что взять", "полис"):
    LEXICON[_stem] = "ADDRESS"
for _stem in ("направлени",):
    LEXICON[_stem] = "REFERRAL"
for _stem in ("оператор", "человек", "регистратор", "соедини", "переключи", "жалоб", "жаловат", "претензи", "острая боль",
              "острой боли", "задыха", "скорая", "скорую", "лекарств", "таблет", "дозиров", "рецепт", "симптом", "диагноз",
              "больничн", "результат"):
    LEXICON[_stem] = "HUMAN"

_PRIORITY = ("HUMAN", "REFERRAL", "RESCHEDULE", "CANCEL", "CONFIRM", "MYBOOKING", "BOOK", "HOLIDAYS", "SCHEDULE", "ADDRESS")

# (state, token class) → state.  Missing pair = self-loop (token ignored).
TRANSITIONS: dict[tuple[str, str], str] = {
    ("start", "BOOK"): "intent", ("start", "RESCHEDULE"): "intent", ("start", "CANCEL"): "intent",
    ("start", "CONFIRM"): "intent", ("start", "MYBOOKING"): "intent", ("start", "SCHEDULE"): "intent",
    ("start", "HOLIDAYS"): "intent", ("start", "HUMAN"): "reject",
    ("start", "ADDRESS"): "intent", ("start", "REFERRAL"): "intent", ("intent", "ADDRESS"): "intent", ("intent", "REFERRAL"): "intent",
    ("start", "SPEC"): "intent",  # "мне к кардиологу" – implicit booking
    ("start", "DATE"): "start", ("start", "TIME"): "start", ("start", "DAYPART"): "start",
    ("intent", "SPEC"): "intent", ("intent", "DATE"): "intent", ("intent", "TIME"): "intent", ("intent", "DAYPART"): "intent",
    ("intent", "SURNAME"): "intent", ("intent", "HUMAN"): "reject",
    ("intent", "BOOK"): "intent", ("intent", "RESCHEDULE"): "intent", ("intent", "CANCEL"): "intent",
    ("intent", "CONFIRM"): "intent", ("intent", "MYBOOKING"): "intent", ("intent", "SCHEDULE"): "intent",
    ("intent", "HOLIDAYS"): "intent",
}


@dataclass
class Query:
    kind: str  # find_slots | my_booking | cancel_booking | confirm_booking | reschedule_candidates | clinic_hours | doctor_hours | holidays | clinic_info | referral | escalate
    sql: str
    params: dict[str, object] = field(default_factory=dict)
    needs: list[str] = field(default_factory=list)  # session facts required to run (patient_id, booking_id)
    trace: list[str] = field(default_factory=list)
    escalate: bool = False

    @property
    def ok(self) -> bool:
        return not self.escalate


@dataclass
class _Slots:
    intents: list[str] = field(default_factory=list)
    specialty: Optional[str] = None
    surname: Optional[str] = None
    day: Optional[date] = None
    at: Optional[time] = None
    daypart: Optional[str] = None


def _classes(text: str, interp: Interpretation) -> list[tuple[str, str]]:
    """Scan the utterance once and return (token class, payload) in order."""
    low = " " + text.lower().replace("ё", "е") + " "
    found: list[tuple[int, str, str]] = []
    for stem, cls in LEXICON.items():
        pos = low.find(stem)
        if pos >= 0:
            found.append((pos, cls, stem))
    for name, stems in SPECIALTIES.items():
        for stem in (name,) + stems:
            if not stem:
                continue
            pos = low.find(stem)
            if pos >= 0 and not (stem == "лор" and " лор" not in low) and not (stem == "зуб" and "зубн" in low):
                found.append((pos, "SPEC", name))
                break
    if interp.date:
        found.append((len(low), "DATE", interp.date.isoformat()))
    if interp.time:
        found.append((len(low) + 1, "TIME", interp.time.strftime("%H:%M")))
    if interp.daypart:
        found.append((len(low) + 2, "DAYPART", interp.daypart))
    sur = extract_surname(text)
    if sur:
        found.append((len(low) + 3, "SURNAME", sur))
    found.sort()
    out: list[tuple[str, str]] = []
    seen_spec = False
    for _, cls, payload in found:
        if cls == "SPEC":
            if seen_spec:
                continue
            seen_spec = True
        out.append((cls, payload))
    return out


def run_automaton(text: str, today: Optional[date] = None) -> tuple[str, _Slots, list[str]]:
    today = today or date.today()
    interp = interpret(text, today)
    state = "start"
    slots = _Slots()
    trace: list[str] = []
    for cls, payload in _classes(text, interp):
        nxt = TRANSITIONS.get((state, cls), state)
        trace.append(f"{state} --{cls}--> {nxt}")
        state = nxt
        if state == "reject":
            break
        if cls in _PRIORITY:
            slots.intents.append(cls)
        elif cls == "SPEC":
            slots.specialty = payload
        elif cls == "SURNAME":
            slots.surname = payload
        elif cls == "DATE":
            slots.day = date.fromisoformat(payload)
        elif cls == "TIME":
            slots.at = time.fromisoformat(payload)
        elif cls == "DAYPART":
            slots.daypart = payload
    return state, slots, trace


def _window(slots: _Slots) -> tuple[Optional[str], Optional[str]]:
    if slots.at:
        t_from = slots.at.strftime("%H:%M")
        t_to = (time((slots.at.hour + 2) % 24, slots.at.minute)).strftime("%H:%M")
        return t_from, t_to
    if slots.daypart:
        return DAYPART_WINDOWS[slots.daypart]
    return None, None


def compile_query(text: str, today: Optional[date] = None) -> Query:
    today = today or date.today()
    state, slots, trace = run_automaton(text, today)
    if state == "reject":
        return Query("escalate", "", {}, [], trace, escalate=True)
    if state != "intent":
        return Query("escalate", "", {}, [], trace + ["no intent reached"], escalate=True)
    intent = next((p for p in _PRIORITY if p in slots.intents), "BOOK" if slots.specialty else None)
    if intent is None:
        return Query("escalate", "", {}, [], trace + ["no intent"], escalate=True)
    if intent == "RESCHEDULE" and "CANCEL" in slots.intents and slots.intents.index("CANCEL") < slots.intents.index("RESCHEDULE"):
        intent = "RESCHEDULE"

    if intent in ("BOOK", "RESCHEDULE"):
        if slots.surname and not slots.specialty:
            return Query("escalate", "", {}, [], trace + ["surname without specialty → human"], escalate=True)
        if not slots.specialty and intent == "BOOK":
            return Query("find_slots", "", {}, ["specialty"], trace + ["missing specialty"], escalate=False)
        where = ["s.status = 'free'", "s.starts_at >= :not_before"]
        params: dict[str, object] = {"not_before": (today + timedelta(days=0)).isoformat()}
        if slots.specialty:
            where.append("d.specialty = :specialty")
            params["specialty"] = slots.specialty
        if slots.surname:
            where.append("lower(d.last_name) LIKE :surname")
            params["surname"] = slots.surname.lower().rstrip("уаойеым")[:5] + "%"
        if slots.day:
            where.append("date(s.starts_at) = :day")
            params["day"] = slots.day.isoformat()
        t_from, t_to = _window(slots)
        if t_from:
            where.append("time(s.starts_at) >= :t_from AND time(s.starts_at) < :t_to")
            params["t_from"], params["t_to"] = t_from, t_to
        sql = (
            "SELECT s.id, s.doctor_id, d.full_name, d.specialty, s.starts_at FROM slots s "
            "JOIN doctors d ON d.id = s.doctor_id WHERE " + " AND ".join(where) + " ORDER BY s.starts_at LIMIT 10"
        )
        kind = "find_slots" if intent == "BOOK" else "reschedule_candidates"
        needs = [] if intent == "BOOK" else ["booking_id"]
        return Query(kind, sql, params, needs, trace)
    if intent == "CANCEL":
        return Query(
            "cancel_booking",
            "UPDATE bookings SET status = 'cancelled', updated_at = :now WHERE id = :booking_id AND patient_id = :patient_id AND status = 'booked'",
            {"now": today.isoformat()}, ["booking_id", "patient_id"], trace,
        )
    if intent == "CONFIRM":
        return Query(
            "confirm_booking",
            "UPDATE bookings SET confirmed = 1, updated_at = :now WHERE id = :booking_id AND patient_id = :patient_id AND status = 'booked'",
            {"now": today.isoformat()}, ["booking_id", "patient_id"], trace,
        )
    if intent == "MYBOOKING":
        return Query(
            "my_booking",
            "SELECT b.id, b.starts_at, d.full_name, d.specialty, d.cabinet FROM bookings b JOIN doctors d ON d.id = b.doctor_id "
            "WHERE b.patient_id = :patient_id AND b.status = 'booked' AND b.starts_at >= :now ORDER BY b.starts_at LIMIT 1",
            {"now": today.isoformat()}, ["patient_id"], trace,
        )
    if intent == "SCHEDULE":
        if slots.specialty:
            return Query(
                "doctor_hours",
                "SELECT d.full_name, date(s.starts_at) AS day, min(time(s.starts_at)) AS first, max(time(s.starts_at)) AS last "
                "FROM slots s JOIN doctors d ON d.id = s.doctor_id WHERE d.specialty = :specialty AND date(s.starts_at) >= :day "
                "GROUP BY d.full_name, day ORDER BY day LIMIT 14",
                {"specialty": slots.specialty, "day": (slots.day or today).isoformat()}, [], trace,
            )
        params = {}
        where = ""
        if slots.day:
            where = " WHERE weekday = :weekday"
            params["weekday"] = slots.day.weekday()
        return Query("clinic_hours", "SELECT weekday, opens, closes FROM clinic_hours" + where + " ORDER BY weekday", params, [], trace)
    if intent == "HOLIDAYS":
        return Query(
            "holidays", "SELECT day, note FROM holidays WHERE day >= :day ORDER BY day LIMIT 5", {"day": today.isoformat()}, [], trace
        )
    if intent == "ADDRESS":
        # the address / document list is a static clinic card; the opening hours come from the mirror
        return Query("clinic_info", "SELECT weekday, opens, closes FROM clinic_hours ORDER BY weekday", {}, [], trace + ["static clinic card + hours"])
    if intent == "REFERRAL":
        if not slots.specialty:
            return Query("referral", "", {}, ["specialty"], trace + ["missing specialty"])
        return Query(
            "referral", "SELECT specialty, max(needs_referral) AS needs_referral FROM doctors WHERE specialty = :specialty GROUP BY specialty",
            {"specialty": slots.specialty}, [], trace,
        )
    return Query("escalate", "", {}, [], trace + ["unhandled"], escalate=True)  # pragma: no cover

"""CRM module: scenario ``call crm.<op>`` → (status, payload).

Every operation is a small, explicit function; the payload only contains what
the prompts need (names, spoken dates/times, ids) – never SNILS / birth date /
phone, so prompts and logs stay free of explicit personal data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

from calls.nlu.intent import REFERRAL_REQUIRED
from calls.tts.verbalize import WEEKDAY_NOM, date_to_words, time_to_words

from .registry import DAYPART_WINDOWS, MockRegistry, Patient, RegistryError, Slot, SlotTaken, normalize_phone

Result = tuple[str, dict]

_SPECIALTY_DATIVE = {
    "терапевт": "терапевту", "педиатр": "педиатру", "хирург": "хирургу", "кардиолог": "кардиологу", "невролог": "неврологу",
    "офтальмолог": "офтальмологу", "отоларинголог": "отоларингологу", "стоматолог": "стоматологу", "гинеколог": "гинекологу",
    "уролог": "урологу", "дерматолог": "дерматологу", "эндокринолог": "эндокринологу", "гастроэнтеролог": "гастроэнтерологу",
    "травматолог": "травматологу", "ортопед": "ортопеду", "онколог": "онкологу", "психиатр": "психиатру", "психотерапевт": "психотерапевту",
    "ревматолог": "ревматологу", "пульмонолог": "пульмонологу", "аллерголог": "аллергологу", "инфекционист": "инфекционисту",
    "физиотерапевт": "физиотерапевту", "узи": "на УЗИ", "флюорография": "на флюорографию", "лаборатория": "в лабораторию",
    "прививка": "на прививку", "массаж": "на массаж",
}


def specialty_dative(s: str) -> str:
    return _SPECIALTY_DATIVE.get(s, s)


def slot_payload(slot: Slot, prefix: str = "slot") -> dict:
    return {
        f"{prefix}_id": slot.id,
        f"{prefix}_date": slot.day.isoformat(),
        f"{prefix}_time": slot.clock.strftime("%H:%M"),
        f"{prefix}_date_spoken": date_to_words(slot.day),
        f"{prefix}_time_spoken": time_to_words(slot.clock),
        "doctor": slot.doctor,
        "doctor_id": slot.doctor_id,
        "cabinet": slot.cabinet,
        "specialty": slot.specialty,
        "specialty_dative": specialty_dative(slot.specialty),
    }


@dataclass
class CrmModule:
    reg: MockRegistry
    today: Callable[[], date] = date.today
    on_write: Optional[Callable[[str, dict], None]] = None  # hook → remote API outbox
    max_day_offers: int = 3
    writes: list[tuple[str, dict]] = field(default_factory=list)

    # --- dispatch ----------------------------------------------------------------------

    def handle(self, op: str, args: dict) -> Result:
        fn = getattr(self, "op_" + op, None)
        if fn is None:
            return "error", {"error": f"unknown op {op}"}
        try:
            from .availability import request_constraints
            with request_constraints(args.get("availability_constraints")):
                return fn(**{k: v for k, v in args.items() if v is not None})
        except SlotTaken as exc:
            return "taken", {"error": str(exc)}
        except RegistryError as exc:
            return "error", {"error": str(exc)}
        except (TypeError, ValueError) as exc:
            return "error", {"error": f"bad args for {op}: {exc}"}

    def _write(self, kind: str, payload: dict) -> None:
        self.writes.append((kind, payload))
        if self.on_write:
            self.on_write(kind, payload)

    # --- identification ------------------------------------------------------------------

    def op_identity_start(self,**args):
        from clinic.identity_service import IdentityService
        with self.reg._lock:
            if not hasattr(self,'_identity_form'):self._identity_form=IdentityService(self.reg,self.today)
        return self._identity_form.start(**args)

    def op_identity_answer(self,**args):
        if not hasattr(self,'_identity_form'):return 'error',{'error':'identity_not_started'}
        return self._identity_form.answer(**args)

    def op_identity_use_policy(self,**args):
        if not hasattr(self,'_identity_form'):return 'error',{'error':'identity_not_started'}
        return self._identity_form.use_policy(**args)

    def op_verify_booking_identity(self,patient_id,booking_id,**_):
        booking=self.reg.booking(int(booking_id))
        if not booking or booking.patient_id!=int(patient_id):return 'mismatch',{}
        return 'ok',{'booking_id':booking.id}

    def _patient_payload(self, p: Patient) -> dict:
        return {"patient_id": p.id, "patient_name": p.short_name, "patient_full_name": p.full_name, "patient_birth_date": p.birth_date.isoformat() if p.birth_date else None}

    def op_identify(self, full_name='', birth_date='', card_no='', locked_patient_id=None, **_):
        import re
        bd=_parse_birth(birth_date)
        if bd is None:return 'empty',{}
        if card_no:
            rows=self.reg.find_patient(card_no=str(card_no),birth_date=bd)
        else:
            name=re.sub(r'^(?:меня зовут|моя фамилия|это)\s+','',str(full_name).casefold().strip()).strip(' .!')
            name=' '.join(name.replace('ё','е').split())
            if len(name.split())!=3:return 'empty',{}
            candidates=self.reg._q('SELECT id FROM patients WHERE birth_date=?',(bd.isoformat(),))
            rows=[self.reg.patient_by_id(r['id']) for r in candidates]
            rows=[p for p in rows if p.full_name.casefold().replace('ё','е')==name]
        if len(rows)!=1:return ('many' if rows else 'empty'),{}
        p=rows[0]
        if locked_patient_id and p.id!=int(locked_patient_id):return 'mismatch',{}
        return 'ok',self._patient_payload(p)

    def op_lookup_phone(self, phone: str = "", **_: object) -> Result:
        pats = self.reg.find_patient(phone=phone) if phone else []
        if not pats:
            return "empty", {"caller_known": False}
        if len(pats) == 1:
            return "ok", {"caller_known": True, **self._patient_payload(pats[0])}
        return "many", {"caller_known": True, "candidates": len(pats)}

    def op_find_patient(self, snils: str = "", card_no: str = "", phone: str = "", birth_date: str = "", **_: object) -> Result:
        bd = _parse_birth(birth_date) if birth_date else None
        pats = self.reg.find_patient(snils=snils or None, card_no=card_no or None, phone=normalize_phone(phone) if phone else None, birth_date=bd)
        if not pats:
            return "empty", {}
        if len(pats) > 1:
            return "many", {"candidates": len(pats)}
        return "ok", self._patient_payload(pats[0])

    def op_verify_birth(self, patient_id: int, birth_date: str = "", **_: object) -> Result:
        p = self.reg.patient_by_id(int(patient_id))
        bd = _parse_birth(birth_date)
        if p is None or bd is None:
            return "error", {"error": "no patient or bad date"}
        return ("ok", self._patient_payload(p)) if p.birth_date == bd else ("mismatch", {})

    # --- slots -----------------------------------------------------------------------------

    def op_find_slots(self, specialty: str, said_date: str = "", said_daypart: str = "", said_time: str = "", **_: object) -> Result:
        day = date.fromisoformat(said_date) if said_date else None
        t_from = t_to = None
        if said_daypart:
            t_from, t_to = DAYPART_WINDOWS[said_daypart]
        elif said_time:
            t_from = said_time
        slots = self.reg.find_slots(specialty, day, t_from=t_from, t_to=t_to, limit=1)
        if not slots and (day or t_from):
            # relax: nearest anything for the specialty
            slots = self.reg.find_slots(specialty, limit=1)
            if slots:
                return "relaxed", {**slot_payload(slots[0]), "needs_referral": specialty in REFERRAL_REQUIRED, "offers": 1}
        if not slots:
            return "empty", {"specialty": specialty, "specialty_dative": specialty_dative(specialty)}
        return "ok", {**slot_payload(slots[0]), "needs_referral": specialty in REFERRAL_REQUIRED, "offers": 1}

    def op_next_day(self, specialty: str, after: str, offers: int = 0, **_: object) -> Result:
        """Next day with free slots after ``after``; caps the "no → next day" loop."""
        offers = int(offers or 0) + 1
        if offers > self.max_day_offers:
            return "too_many", {"offers": offers}
        day = self.reg.next_day(specialty, date.fromisoformat(after))
        if day is None:
            return "empty", {"offers": offers}
        slot = self.reg.find_slots(specialty, day, limit=1)[0]
        return "ok", {**slot_payload(slot), "offers": offers, "day": day.isoformat(), "day_spoken": date_to_words(day)}

    def op_check_day(self, specialty: str, said_date: str, **_: object) -> Result:
        day = date.fromisoformat(said_date)
        if day < self.today():
            return "past", {"day_spoken": date_to_words(day)}
        slots = self.reg.find_slots(specialty, day, limit=1)
        if slots:
            return "ok", {**slot_payload(slots[0]), "day": day.isoformat(), "day_spoken": date_to_words(day)}
        nxt = self.reg.next_day(specialty, day)
        if nxt is None:
            return "empty", {"day_spoken": date_to_words(day)}
        slot = self.reg.find_slots(specialty, nxt, limit=1)[0]
        return "other_day", {**slot_payload(slot), "day": nxt.isoformat(), "day_spoken": date_to_words(nxt), "asked_day_spoken": date_to_words(day)}

    def op_day_parts(self, specialty: str, day: str, **_: object) -> Result:
        parts = self.reg.day_parts(specialty, date.fromisoformat(day))
        if not parts:
            return "empty", {}
        if len(parts) == 1:
            a, b = ("00:00", "13:00") if parts[0] == "morning" else ("13:00", "23:59")
            slot = self.reg.find_slots(specialty, date.fromisoformat(day), t_from=a, t_to=b, limit=1)[0]
            return "single", {**slot_payload(slot), "part": parts[0], "part_spoken": "до обеда" if parts[0] == "morning" else "после обеда"}
        return "both", {"parts": ",".join(parts)}

    def op_nearest_in_day_part(self, specialty: str, day: str, said_daypart: str = '', **_: object) -> Result:
        d = date.fromisoformat(day)
        a, b = ("00:00", "13:00") if said_daypart == "morning" else ("13:00", "23:59")
        if said_daypart in ("midday", "evening"):
            a, b = DAYPART_WINDOWS[said_daypart]
        slots = self.reg.find_slots(specialty, d, t_from=a, t_to=b, limit=1)
        if slots:
            return "ok", slot_payload(slots[0])
        return "empty", {}

    def op_list_slots(self, specialty: str, day: str, said_daypart: str = "", **_: object) -> Result:
        d = date.fromisoformat(day)
        t_from = t_to = None
        if said_daypart:
            t_from, t_to = ("00:00", "13:00") if said_daypart == "morning" else ("13:00", "23:59")
        slots = self.reg.find_slots(specialty, d, t_from=t_from, t_to=t_to, limit=6)
        if not slots:
            return "empty", {}
        times = [s.clock.strftime("%H:%M") for s in slots]
        spoken = ", ".join(time_to_words(s.clock) for s in slots)
        return "ok", {"slot_times": ",".join(times), "slots_spoken": spoken, "slots_count": len(slots)}

    def op_pick_time(self, specialty: str, day: str, said_time: str = '', **_: object) -> Result:
        d = date.fromisoformat(day)
        if not said_time:
            from .availability import current
            spec = current()
            if spec.get('time_from') is None and spec.get('time_to') is None:
                return 'error', {'error':'time_required'}
            said_time = spec.get('time_from') or '00:00'
        t = time.fromisoformat(said_time)
        exact = self.reg.find_slots(specialty, d, t_from=t.strftime("%H:%M"), t_to=(datetime.combine(d, t) + timedelta(minutes=1)).time().strftime("%H:%M"), limit=1)
        if exact:
            return "ok", slot_payload(exact[0])
        near = self.reg.find_slots(specialty, d, t_from=t.strftime("%H:%M"), limit=1)
        if near:
            return "near", slot_payload(near[0])
        return "empty", {}

    def op_nearest_in_part(self, specialty: str, said_daypart: str = '', from_day: str = '', **_: object) -> Result:
        slot = self.reg.nearest_in_part(specialty, said_daypart, date.fromisoformat(from_day))
        if slot is None:
            return "empty", {}
        return "ok", slot_payload(slot)

    # --- bookings ----------------------------------------------------------------------------

    def op_book(self, patient_id: int, slot_id: int, call_id: str = "", **_: object) -> Result:
        b = self.reg.book(int(patient_id), int(slot_id))
        self._write("book", {"booking_id": b.id, "patient_id": b.patient_id, "slot_id": b.slot_id, "doctor_id": b.doctor_id, "starts_at": b.starts_at.isoformat(), "call_id": call_id})
        return "ok", {"booking_id": b.id, **slot_payload(self.reg.slot(b.slot_id))}

    def _booking_payload(self,b):
        return {
            "booking_id": b.id, "doctor": b.doctor, "specialty": b.specialty, "specialty_dative": specialty_dative(b.specialty), "cabinet": b.cabinet,
            "slot_id": b.slot_id, "slot_date": b.starts_at.date().isoformat(), "slot_time": b.starts_at.strftime("%H:%M"),
            "slot_date_spoken": date_to_words(b.starts_at.date()), "slot_time_spoken": time_to_words(b.starts_at.time()), "confirmed": b.confirmed,
        }

    def op_my_booking(self, patient_id: int, **_: object) -> Result:
        rows=self.reg._q("SELECT id FROM bookings WHERE patient_id=? AND status='booked' AND starts_at>=? ORDER BY starts_at,id LIMIT 4",
            (int(patient_id),self.reg.now().isoformat(timespec='minutes')))
        if not rows:return 'empty',{}
        if len(rows)>1:
            options=[self.reg.booking(r['id']) for r in rows[:3]]
            return 'many',{'booking_candidate_ids':[b.id for b in options], 'booking_id':None,
                'booking_options':'. '.join(f'{i+1}: {b.specialty}, {date_to_words(b.starts_at.date())}, {time_to_words(b.starts_at.time())}' for i,b in enumerate(options))}
        return 'ok',self._booking_payload(self.reg.booking(rows[0]['id']))

    def op_select_booking(self,patient_id,choices,index,**_):
        if not isinstance(choices,list) or not 1<=len(choices)<=3:return 'error',{}
        position=int(index)-1
        if position<0 or position>=len(choices):return 'error',{}
        b=self.reg.booking(int(choices[position]))
        if not b or b.patient_id!=int(patient_id) or b.status!='booked' or b.starts_at<self.reg.now():return 'error',{}
        return 'ok',self._booking_payload(b)

    def op_cancel(self, booking_id: int, call_id: str = "", **_: object) -> Result:
        b = self.reg.cancel(int(booking_id))
        self._write("cancel", {"booking_id": b.id, "call_id": call_id})
        return "ok", {"booking_id": b.id, "booking_status": b.status}

    def op_confirm(self, booking_id: int, call_id: str = "", **_: object) -> Result:
        b = self.reg.confirm(int(booking_id))
        self._write("confirm", {"booking_id": b.id, "call_id": call_id})
        return "ok", {"booking_id": b.id, "confirmed": True}

    def op_reschedule(self, booking_id: int, slot_id: int, call_id: str = "", **_: object) -> Result:
        new = self.reg.reschedule(int(booking_id), int(slot_id))
        self._write("reschedule", {"old_booking_id": int(booking_id), "booking_id": new.id, "patient_id": new.patient_id, "slot_id": new.slot_id,
                                   "doctor_id": new.doctor_id, "starts_at": new.starts_at.isoformat(), "call_id": call_id})
        return "ok", {"booking_id": new.id, **slot_payload(self.reg.slot(new.slot_id))}

    # --- stand input: bookings that already exist in the stand -----------------------------------

    def op_import_booking(self, remote_id: str, patient_id: int, starts_at: str, doctor: object = 1, confirmed: bool = False, **_: object) -> Result:
        """Mirror a registrar-made booking (stand id → local id).  Idempotent."""
        bid = self.reg.import_booking(str(remote_id), int(patient_id), doctor if isinstance(doctor, int) else str(doctor), datetime.fromisoformat(str(starts_at)), bool(confirmed))
        return "ok", {"booking_id": bid, "remote_id": str(remote_id)}

    def op_reminder_vars(self, remote_id: str = "", booking_id: int = 0, **_: object) -> Result:
        """Variables for the outbound ``reminder`` flow (same shape as ``calls.outbound.reminder.build_campaign``)."""
        b = self.reg.booking_by_remote_id(str(remote_id)) if remote_id else self.reg.booking(int(booking_id))
        if b is None:
            return "empty", {}
        p = self.reg.patient_by_id(b.patient_id)
        if p is None:
            return "empty", {}
        return "ok", {
            "patient_id": p.id, "patient_name": p.short_name, "booking_id": b.id, "specialty": b.specialty, "specialty_dative": specialty_dative(b.specialty),
            "doctor": b.doctor, "cabinet": b.cabinet, "slot_id": b.slot_id, "slot_date": b.starts_at.date().isoformat(), "slot_time": b.starts_at.strftime("%H:%M"),
            "slot_date_spoken": date_to_words(b.starts_at.date()), "slot_time_spoken": time_to_words(b.starts_at.time()), "today": self.today().isoformat(),
        }

    # --- info ----------------------------------------------------------------------------------

    def op_clinic_info(self, said_date: str = "", **_: object) -> Result:
        wd = date.fromisoformat(said_date).weekday() if said_date else None
        rows = self.reg.clinic_hours(wd)
        if wd is not None and not rows:
            return "ok", {"info_text": f"{WEEKDAY_NOM[wd].capitalize()} — выходной день"}
        if wd is not None:
            _, o, c = rows[0]
            return "ok", {"info_text": f"{WEEKDAY_NOM[wd].capitalize()}: с {time_to_words(time.fromisoformat(o))} до {time_to_words(time.fromisoformat(c))}"}
        parts = []
        for w, o, c in rows:
            parts.append(f"{WEEKDAY_NOM[w]} с {time_to_words(time.fromisoformat(o))} до {time_to_words(time.fromisoformat(c))}")
        return "ok", {"info_text": "Поликлиника работает: " + "; ".join(parts) + ". Воскресенье — выходной."}

    def op_holidays(self, **_: object) -> Result:
        hs = self.reg.holidays(self.today())
        if not hs:
            return "ok", {"info_text": "В ближайшее время праздничных дней нет, поликлиника работает по обычному графику."}
        return "ok", {"info_text": " ".join(f"{date_to_words(d, with_weekday=False)}: {note}." for d, note in hs)}

    def op_referral_required(self, specialty: str, **_: object) -> Result:
        payload = {"specialty_dative": specialty_dative(specialty), "needs_referral": specialty in REFERRAL_REQUIRED}
        return ("required" if specialty in REFERRAL_REQUIRED else "not_required"), payload


def _parse_birth(raw: str) -> Optional[date]:
    """DTMF ДДММГГГГ or ISO."""
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        try:
            return date(int(raw[4:]), int(raw[2:4]), int(raw[:2]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None

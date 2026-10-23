"""End-to-end "мясорубка": text → (simulated ASR) → normalise → classify → switch-case → CRM writes.

The invariant that matters for the case ("zero booking errors") is checked
exhaustively: whatever the recogniser does to the sentence, the robot either
performs exactly the requested operation or hands over to a human – it never
books the wrong specialty, the wrong day part or cancels the wrong booking.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from calls.crm.registry import DAYPART_WINDOWS
from calls.orchestrator.simulate import run_call
from tests.asr_noise import corrupt
from tests.conftest import TODAY


def states(tr):
    return [s.split(".")[-1] for s in tr.states]


# ---- DTMF-only paths -------------------------------------------------------------------------


def test_dtmf_booking_known_phone(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, ["1", "1", ("digits", "12051980"), "1", "1"], vars=known_caller, today=TODAY)
    assert tr.status == "booked" and tr.transfer is None and tr.warnings == [] and tr.unused_inputs == []
    assert [c[1] for c in tr.module_calls] == ["lookup_phone", "find_patient", "referral_required", "find_slots", "book"]
    b = crm.reg.booking(tr.vars["booking_id"])
    assert b.patient_id == 1 and b.specialty == "терапевт" and b.status == "booked"
    assert tr.sms[0]["template"] == "booked" and tr.sms[0]["vars"]["patient_name"] == "Иван Иванович"
    assert "Повторяю" in tr.prompts[-1] and "Иван Иванович" in tr.prompts[-1]
    # the slot is actually taken in the mirror
    assert crm.reg.slot(b.slot_id) is not None and crm.reg.find_slots("терапевт", limit=1)[0].id != b.slot_id


def test_dtmf_booking_wrong_birth_date_falls_back_to_snils(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, ["1", "1", ("digits", "01011999"), "1", ("digits", "12345678901"), "3", "1"], vars=known_caller, today=TODAY)
    assert tr.status == "booked"
    assert "id_birth_mismatch" in states(tr) and "id_ask_snils" in states(tr)
    assert crm.reg.booking(tr.vars["booking_id"]).specialty == "хирург"


def test_dtmf_booking_for_other_person_by_card(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, ["1", "2", "2", ("digits", "1002#"), "2", "1"], vars=known_caller, today=TODAY)
    assert tr.status == "booked" and tr.vars["for_other"] is True
    b = crm.reg.booking(tr.vars["booking_id"])
    assert b.patient_id == 2 and b.specialty == "педиатр"
    assert tr.sms[0]["vars"]["patient_name"] == "Анна Ивановна"  # the *booked person's* name, not the caller's


def test_unknown_phone_second_not_found_goes_to_operator(flows, crm):
    tr = run_call(flows, "inbound", crm, ["1", "1", ("digits", "00000000000"), "1", ("digits", "00000000001")], vars={"caller_phone": "70000000000", "today": TODAY.isoformat()}, today=TODAY)
    assert tr.status == "transfer" and tr.transfer["reason"] == "patient_not_found"
    assert crm.writes == []


def test_snils_entry_star_clears_buffer(flows, crm):
    tr = run_call(flows, "inbound", crm, ["1", "1", ("digits", "999*12345678901"), "1", "1"], vars={"caller_phone": "70000000000", "today": TODAY.isoformat()}, today=TODAY)
    assert tr.status == "booked" and crm.reg.booking(tr.vars["booking_id"]).patient_id == 1


def test_three_invalid_keys_go_to_operator(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, ["7", "8", "9"], vars=known_caller, today=TODAY)
    assert tr.status == "transfer" and tr.transfer["reason"] == "no_valid_input" and crm.writes == []


def test_info_menu_without_identification(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, ["3", "2", "0"], vars=known_caller, today=TODAY)
    assert tr.status == "done" and "ноября" in tr.prompts[2] and [c[1] for c in tr.module_calls] == ["holidays"]


def test_cancel_requires_confirmation_and_frees_slot(flows, crm, known_caller):
    slot = crm.reg.find_slots("терапевт", date(2026, 10, 15))[0]
    b = crm.reg.book(1, slot.id)
    tr = run_call(flows, "inbound", crm, ["2", "1", ("digits", "12051980"), "0", "2", "1"], vars=known_caller, today=TODAY)
    # pressed 0 (cancel) → 2 (keep) → back to menu → 1 (confirm)
    assert tr.status == "confirmed" and crm.reg.booking(b.id).confirmed and crm.reg.booking(b.id).status == "booked"
    tr = run_call(flows, "inbound", crm, ["2", "1", ("digits", "12051980"), "0", "1"], vars=known_caller, today=TODAY)
    assert tr.status == "cancelled" and crm.reg.booking(b.id).status == "cancelled"
    assert crm.reg.slot(slot.id) and crm.reg.find_slots("терапевт", date(2026, 10, 15))[0].id == slot.id  # slot free again
    assert tr.sms[-1]["template"] == "cancelled"


def test_referral_required_specialist_asks_for_referral(flows, crm, known_caller):
    # 6 → say "к кардиологу" → referral question → 2 (no referral) → therapist booked instead
    tr = run_call(flows, "inbound", crm, ["1", "1", ("digits", "12051980"), "6", ("say", "к кардиологу"), "2", "1"], vars=known_caller, today=TODAY)
    assert tr.status == "booked" and "book_ask_referral" in states(tr)
    assert crm.reg.booking(tr.vars["booking_id"]).specialty == "терапевт"
    # with referral → cardiologist booked and flagged
    tr = run_call(flows, "inbound", crm, ["1", "1", ("digits", "12051980"), "6", ("say", "к кардиологу"), "1", "1"], vars=known_caller, today=TODAY)
    assert tr.status == "booked" and tr.vars["has_referral"] is True
    assert crm.reg.booking(tr.vars["booking_id"]).specialty == "кардиолог"


# ---- speech paths ----------------------------------------------------------------------------


def test_sixty_seconds_silence_then_speech_booking(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, [("timeout",), ("say", "хочу записаться к хирургу в пятницу после обеда"), "1", ("digits", "12051980"), "1"], vars=known_caller, today=TODAY)
    assert states(tr)[:3] == ["welcome", "listen_free", "intent_book"]
    assert tr.status == "booked"
    b = crm.reg.booking(tr.vars["booking_id"])
    assert b.specialty == "хирург" and b.starts_at.date() == date(2026, 10, 16) and b.starts_at.time() >= time(13, 0)
    assert "book_start" not in states(tr)  # the DTMF menu is skipped: the specialty was already said


def test_speech_without_intent_goes_to_operator_not_silent_caller(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, [("timeout",), ("say", "алло алло вы меня слышите")], vars=known_caller, today=TODAY)
    assert tr.status == "transfer" and tr.transfer["reason"] == "speech_unclassified"
    tr = run_call(flows, "inbound", crm, [("timeout",), ("silence",)], vars=known_caller, today=TODAY)
    assert tr.status == "no_input" and tr.transfer is None


@pytest.mark.parametrize("text,reason", [
    ("у меня острая боль в животе", "human_only"), ("хочу пожаловаться на врача", "human_only"),
    ("можно ли пить эти таблетки вместе", "human_only"), ("соедините с оператором", "human_only"),
])
def test_human_only_intents(flows, crm, known_caller, text, reason):
    tr = run_call(flows, "inbound", crm, [("timeout",), ("say", text)], vars=known_caller, today=TODAY)
    assert tr.status == "transfer" and tr.transfer["reason"] == reason and crm.writes == []


def test_specific_doctor_by_surname_goes_to_operator_with_context(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, ["1", "1", ("digits", "12051980"), "6", ("say", "к доктору Волкову на следующей неделе")], vars=known_caller, today=TODAY)
    assert tr.status == "transfer" and tr.transfer["reason"] == "specific_doctor"
    assert tr.transfer["vars"]["doctor_surname"] == "Волкову" and tr.transfer["vars"]["patient_id"] == 1


def test_offer_other_day_by_voice(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, ["1", "1", ("digits", "12051980"), "1", ("say", "а можно в понедельник"), ("say", "да")], vars=known_caller, today=TODAY)
    assert tr.status == "booked" and crm.reg.booking(tr.vars["booking_id"]).starts_at.date() == date(2026, 10, 19)


def test_three_refusals_of_next_day_go_to_operator(flows, crm, known_caller):
    tr = run_call(flows, "inbound", crm, ["1", "1", ("digits", "12051980"), "1", "2", "2", "2", "2"], vars=known_caller, today=TODAY)
    assert tr.status == "transfer" and tr.transfer["reason"] == "no_suitable_day" and crm.writes == []


# ---- reminder / reschedule -------------------------------------------------------------------


def reminder_vars(crm, known_caller):
    slot = crm.reg.find_slots("терапевт", date(2026, 10, 15))[0]
    b = crm.reg.book(1, slot.id)
    return b, {**known_caller, "patient_id": 1, "patient_name": "Иван Иванович", "booking_id": b.id, "specialty": "терапевт", "specialty_dative": "терапевту",
               "doctor": b.doctor, "cabinet": b.cabinet, "slot_date": b.starts_at.date().isoformat(), "slot_time": b.starts_at.strftime("%H:%M"),
               "slot_date_spoken": "пятнадцатого октября", "slot_time_spoken": "восемь часов"}


def test_reminder_confirm_cancel_noanswer(flows, crm, known_caller):
    b, v = reminder_vars(crm, known_caller)
    assert run_call(flows, "reminder", crm, ["1"], vars=dict(v), today=TODAY).status == "confirmed"
    assert crm.reg.booking(b.id).confirmed
    tr = run_call(flows, "reminder", crm, [("timeout",), ("timeout",)], vars=dict(v), today=TODAY)
    assert tr.status == "no_answer" and crm.reg.booking(b.id).status == "booked"
    tr = run_call(flows, "reminder", crm, [("say", "нет, не приду"), ("say", "да")], vars=dict(v), today=TODAY)
    assert tr.status == "cancelled" and crm.reg.booking(b.id).status == "cancelled"
    assert "Мы никогда не просим коды из СМС" in tr.prompts[0]


def test_reminder_reschedule_full_dialogue(flows, crm, known_caller):
    b, v = reminder_vars(crm, known_caller)
    tr = run_call(flows, "reminder", crm, ["2", ("say", "нет"), ("say", "да, подходит"), ("say", "после обеда"), ("say", "нет, не подходит"), ("say", "в пятнадцать тридцать")], vars=dict(v), today=TODAY)
    assert tr.status == "rescheduled"
    assert states(tr) == ["start", "rs_start", "rs_offer_day", "rs_next_day", "rs_offer_day", "rs_day_parts", "rs_ask_part", "rs_nearest_in_part", "rs_offer_time",
                          "rs_list_slots", "rs_say_slots", "rs_pick_time", "rs_do", "rs_done"]
    new = crm.reg.booking(tr.vars["booking_id"])
    assert new.starts_at == datetime(2026, 10, 16, 15, 30) and crm.reg.booking(b.id).status == "rescheduled"
    assert tr.sms[0]["template"] == "rescheduled" and "Повторяю" in tr.prompts[-1]


def test_reminder_reschedule_daypart_answer_finds_nearest_day_with_that_part(flows, crm, known_caller):
    b, v = reminder_vars(crm, known_caller)
    tr = run_call(flows, "reminder", crm, ["2", ("say", "да"), "2", ("say", "нет"), ("say", "лучше вечером"), ("say", "да")], vars=dict(v), today=TODAY)
    assert tr.status == "rescheduled"
    new = crm.reg.booking(tr.vars["booking_id"])
    assert new.starts_at.time() >= time(17, 0)


def test_reminder_reschedule_non_date_answer_goes_to_human_with_data(flows, crm, known_caller):
    b, v = reminder_vars(crm, known_caller)
    tr = run_call(flows, "reminder", crm, ["2", ("say", "а какой врач будет принимать")], vars=dict(v), today=TODAY)
    assert tr.status == "transfer" and tr.transfer["reason"] == "not_a_date"
    assert tr.transfer["vars"]["booking_id"] == b.id and tr.transfer["vars"]["patient_id"] == 1
    assert crm.reg.booking(b.id).status == "booked"  # data kept, nothing changed


def test_reminder_reschedule_said_date_without_slots_offers_nearest(flows, crm, known_caller):
    b, v = reminder_vars(crm, known_caller)
    tr = run_call(flows, "reminder", crm, ["2", ("say", "в субботу"), ("say", "да"), "1", "1"], vars=dict(v), today=TODAY)
    assert "rs_offer_other_day" in states(tr) and tr.status == "rescheduled"
    assert crm.reg.booking(tr.vars["booking_id"]).starts_at.date() == date(2026, 10, 19)  # Monday after the Saturday


# ---- the grinder -----------------------------------------------------------------------------

SPECIALTIES = {"терапевт": ["терапевту", "к терапевту"], "хирург": ["хирургу", "к хирургу"], "педиатр": ["педиатру", "детскому врачу"],
               "офтальмолог": ["окулисту", "глазному"], "отоларинголог": ["лору", "к лору"]}
DAYS = {None: ["", "на ближайшее время"], date(2026, 10, 15): ["завтра", "на завтра", "пятнадцатого", "15 октября", "в четверг"],
        date(2026, 10, 16): ["в пятницу", "шестнадцатого октября", "16.10", "послезавтра"], date(2026, 10, 19): ["в понедельник", "девятнадцатого"]}
PARTS = {None: [""], "morning": ["утром", "до обеда", "с утра"], "afternoon": ["после обеда", "во второй половине дня"]}
OPENERS = ["хочу записаться", "запишите меня", "мне нужно попасть", "можно записаться", "нужен талон"]


def gen_cases():
    i = 0
    for spec, forms in SPECIALTIES.items():
        for day, dforms in DAYS.items():
            for part, pforms in PARTS.items():
                i += 1
                f = forms[i % len(forms)]
                d = dforms[i % len(dforms)]
                p = pforms[i % len(pforms)]
                text = " ".join(x for x in (OPENERS[i % len(OPENERS)], f, d, p) if x)
                yield text, spec, day, part


@pytest.mark.parametrize("severity", [0.0, 0.5, 1.0])
def test_grinder_never_books_wrong(flows, crm, known_caller, severity):
    total = booked = 0
    wrong: list[str] = []
    misunderstood: list[str] = []
    for text, spec, day, part in gen_cases():
        for seed in range(3):
            heard = corrupt(text, seed * 7919 + len(text), severity)
            total += 1
            tr = run_call(flows, "inbound", crm, [("timeout",), ("say", heard), "1", ("digits", "12051980"), "1"], vars=known_caller, today=TODAY)
            if tr.status == "booked":
                booked += 1
                b = crm.reg.booking(tr.vars["booking_id"])
                understood_from_speech = "book_start" not in states(tr)
                # Whatever was *understood* must be honoured exactly; what was not understood is covered by the
                # explicit offer ("ближайшая запись: <дата>, <время>") that the caller has to confirm.
                if understood_from_speech and b.specialty != spec:
                    wrong.append(f"{heard!r} → {b.specialty} instead of {spec}")
                if day and tr.vars.get("said_date") == day.isoformat() and b.starts_at.date() != day and tr.module_calls[-2][3] != "relaxed":
                    wrong.append(f"{heard!r} → {b.starts_at} instead of {day}")
                if part and tr.vars.get("said_daypart") == part and tr.module_calls[-2][3] == "ok":
                    a, z = DAYPART_WINDOWS[part]
                    if not (time.fromisoformat(a) <= b.starts_at.time() < time.fromisoformat(z)):
                        wrong.append(f"{heard!r} → {b.starts_at.time()} not in {part}")
                if day and tr.vars.get("said_date") != day.isoformat():
                    misunderstood.append(heard)
                if part and tr.vars.get("said_daypart") != part:
                    misunderstood.append(heard)
                crm.reg.cancel(b.id)
            else:
                # not self-served: handed to a human, or the robot asked a clarifying question the script did not answer
                assert tr.status in ("transfer", None), (heard, tr.status)
                assert not any(w[0] == "book" and w[1].get("patient_id") == 1 and crm.reg.booking(w[1]["booking_id"]).status == "booked" for w in crm.writes), heard
    assert wrong == [], wrong
    share = booked / total
    assert share >= (0.97 if severity == 0 else 0.8), f"only {share:.0%} self-served at severity {severity}"
    # constraints lost to ASR noise are announced back and confirmed, but we still want most of them understood
    assert len(misunderstood) <= total * (0.0 if severity == 0 else 0.15), misunderstood


def test_grinder_cancel_variants_never_cancel_without_confirmation(flows, crm, known_caller):
    phrases = ["отмените мою запись", "я не смогу прийти", "хочу отказаться от записи", "снимите запись", "не приду завтра уберите запись"]
    for i, phrase in enumerate(phrases):
        slot = crm.reg.find_slots("терапевт", [date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19)][i % 3])[0]
        b = crm.reg.book(1, slot.id)
        heard = corrupt(phrase, i, 0.7)
        tr = run_call(flows, "inbound", crm, [("timeout",), ("say", heard), "1", ("digits", "12051980"), ("say", "нет, оставьте"), "9"], vars=known_caller, today=TODAY)
        assert crm.reg.booking(b.id).status == "booked", heard  # "no" at the confirmation keeps the booking
        assert tr.status in ("transfer", None)
        tr = run_call(flows, "inbound", crm, [("timeout",), ("say", heard), "1", ("digits", "12051980"), "1"], vars=known_caller, today=TODAY)
        if tr.status == "cancelled":
            assert crm.reg.booking(b.id).status == "cancelled"
        else:
            assert tr.status == "transfer" and crm.reg.booking(b.id).status == "booked"
            crm.reg.cancel(b.id)
        if crm.reg.booking(b.id).status != "cancelled":
            crm.reg.cancel(b.id)

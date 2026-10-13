"""Тесты слот-движка (``app/assistant/slot_engine.py``).

Проверяем контракт ``docs/ARCHITECTURE.md`` 3.2 и правила записи из
регламента (раздел 5, С-01):

* горизонт 14 календарных дней;
* фильтры «после обеда», «не в пятницу», границы времени;
* слот «сегодня» доступен не позже чем за 30 минут до начала;
* занятые моменты исключаются (``tuple`` и ``Visit``);
* ``limit=3`` по умолчанию;
* модуль поднимается и не падает без собранных ``data/*``.

Движок детерминирован: время задаётся через ``now_provider``, системные часы
внутри логики не используются.

Запуск::

    python tests/test_slots.py
    python -m unittest tests.test_slots -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.assistant.slot_engine import (  # noqa: E402
    CLINIC_HOURS,
    MIN_LEAD_MINUTES,
    SlotEngine,
)
from app.assistant.slot_engine import WAITING_TERMS  # noqa: E402
from app.types import Doctor, SlotQuery, Visit, VisitStatus  # noqa: E402

#: Фиксированное «сейчас» для тестов: понедельник, 09:00.
NOW = datetime(2026, 10, 5, 9, 0)
HORIZON_DAYS = 14


def build_engine(**kwargs) -> SlotEngine:
    kwargs.setdefault("now_provider", lambda: NOW)
    return SlotEngine(**kwargs)


def _to_minutes(text: str) -> int:
    return int(text[:2]) * 60 + int(text[3:])


class SlotEngineReadyTests(unittest.TestCase):
    """Данные собраны: движок отвечает по контракту."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = build_engine()
        if not cls.engine.is_ready:
            raise unittest.SkipTest(
                "data/* не собраны — сначала запустите tools/build_schedule.py"
            )

    def test_doctors_and_specialties(self) -> None:
        doctors = self.engine.doctors()
        self.assertGreaterEqual(len(doctors), 20)
        self.assertTrue(all(isinstance(d, Doctor) for d in doctors))
        self.assertEqual([d.id for d in doctors], sorted(d.id for d in doctors))
        specialties = {d.specialty for d in doctors}
        self.assertGreaterEqual(len(specialties), 10)
        self.assertIn("терапевт", specialties)
        self.assertTrue(all(s == s.lower() for s in specialties))

    def test_doctor_days_and_shift(self) -> None:
        therapist = self.engine.doctors_of_specialty("терапевт")[0]
        days = self.engine.doctor_days(therapist.id)
        self.assertTrue(days, "у терапевта должна быть сетка приёма")
        for weekday, times in days.items():
            self.assertIn(weekday, range(7))
            self.assertTrue(times, f"пустой день недели {weekday}")
            self.assertEqual(list(times), sorted(times))
        shift = self.engine.doctor_shift(therapist.id)
        self.assertIsNotNone(shift)
        self.assertEqual(len(shift), 2)
        self.assertLess(shift[0], shift[1])
        # выходной воскресенья в сетку не попадает
        self.assertNotIn(6, days)
        # неизвестный врач — безопасные пустые значения
        self.assertEqual(self.engine.doctor_days(99_999), {})
        self.assertIsNone(self.engine.doctor_shift(99_999))

    # --- 1. свободные слоты терапевта на 14 дней ---

    def test_therapist_slots_within_horizon(self) -> None:
        query = SlotQuery(specialty="терапевт", limit=0)
        slots = self.engine.find_slots(query)
        self.assertTrue(slots, "должны быть свободные слоты терапевта")
        horizon_end = NOW.date() + timedelta(days=HORIZON_DAYS - 1)
        for slot in slots:
            self.assertEqual(slot.specialty, "терапевт")
            self.assertGreaterEqual(slot.start.date(), NOW.date())
            self.assertLessEqual(slot.start.date(), horizon_end)
            self.assertEqual(slot.start.weekday(), slot.end.weekday())
            self.assertGreater(slot.end, slot.start)
            self.assertIn(slot.start.weekday(), self.engine.doctor_days(slot.doctor_id))

        dates = sorted({slot.start.date() for slot in slots})
        self.assertGreaterEqual(len(dates), 5, "терапевт должен принимать в разные дни")
        self.assertLessEqual((dates[-1] - dates[0]).days, HORIZON_DAYS - 1)

    def test_slots_are_sorted_and_unique(self) -> None:
        slots = self.engine.find_slots(SlotQuery(specialty="терапевт", limit=0))
        keys = [(slot.start, slot.doctor_id) for slot in slots]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)), "дубли слотов недопустимы")

    def test_slots_within_clinic_hours(self) -> None:
        """Сетка и выданные слоты не выходят за часы работы поликлиники (2.2)."""
        for doctor in self.engine.doctors():
            step = self._step_of(doctor.id)
            for weekday, times in self.engine.doctor_days(doctor.id).items():
                window = CLINIC_HOURS[weekday]
                self.assertIsNotNone(window, f"сетка попала в выходной день {weekday}")
                open_minutes = _to_minutes(window[0])
                close_minutes = _to_minutes(window[1])
                for text in times:
                    start_minutes = _to_minutes(text)
                    self.assertGreaterEqual(start_minutes, open_minutes)
                    self.assertLessEqual(start_minutes + step, close_minutes)

        for doctor in self.engine.doctors():
            slots = self.engine.find_slots(SlotQuery(doctor_id=doctor.id, limit=0))
            for slot in slots:
                window = CLINIC_HOURS[slot.start.weekday()]
                self.assertIsNotNone(window)
                self.assertGreaterEqual(_to_minutes(slot.time_text), _to_minutes(window[0]))
                self.assertLessEqual(_to_minutes(slot.end.strftime("%H:%M")), _to_minutes(window[1]))

    def _step_of(self, doctor_id: int) -> int:
        """Шаг сетки врача — из разницы двух соседних времён его расписания."""
        for times in self.engine.doctor_days(doctor_id).values():
            if len(times) >= 2:
                return _to_minutes(times[1]) - _to_minutes(times[0])
        return 20

    # --- 2. фильтр «после обеда» ---

    def test_part_of_day_afternoon(self) -> None:
        query = SlotQuery(specialty="терапевт", part_of_day="afternoon", limit=0)
        slots = self.engine.find_slots(query)
        self.assertTrue(slots)
        for slot in slots:
            self.assertGreaterEqual(slot.start.hour, 12)
            self.assertLess(slot.start.hour, 17)

    def test_part_of_day_morning_and_evening(self) -> None:
        morning = self.engine.find_slots(
            SlotQuery(specialty="терапевт", part_of_day="morning", limit=0)
        )
        self.assertTrue(morning)
        self.assertTrue(all(slot.start.hour < 12 for slot in morning))

        evening = self.engine.find_slots(
            SlotQuery(specialty="терапевт", part_of_day="evening", limit=0)
        )
        self.assertTrue(all(slot.start.hour >= 17 for slot in evening))

    def test_time_from_and_time_to(self) -> None:
        slots = self.engine.find_slots(
            SlotQuery(specialty="терапевт", time_from="13:00", time_to="16:00", limit=0)
        )
        self.assertTrue(slots)
        for slot in slots:
            minutes = slot.start.hour * 60 + slot.start.minute
            self.assertGreaterEqual(minutes, 13 * 60)
            self.assertLessEqual(minutes, 16 * 60)

    # --- 3. «только не в пятницу» ---

    def test_exclude_friday(self) -> None:
        friday = 4
        query = SlotQuery(specialty="терапевт", exclude_weekdays=(friday,), limit=0)
        slots = self.engine.find_slots(query)
        self.assertTrue(slots)
        self.assertTrue(all(slot.start.weekday() != friday for slot in slots))

        # без исключения пятницы в горизонте они есть — фильтр что-то отсекает
        all_slots = self.engine.find_slots(SlotQuery(specialty="терапевт", limit=0))
        self.assertTrue(any(slot.start.weekday() == friday for slot in all_slots))

    def test_weekdays_whitelist(self) -> None:
        monday, wednesday = 0, 2
        slots = self.engine.find_slots(
            SlotQuery(specialty="терапевт", weekdays=(monday, wednesday), limit=0)
        )
        self.assertTrue(slots)
        self.assertTrue(all(slot.start.weekday() in (monday, wednesday) for slot in slots))

    # --- 4. «сегодня не позже чем за 30 минут» ---

    def test_same_day_min_lead_30_minutes(self) -> None:
        therapist = self.engine.doctors_of_specialty("терапевт")[0]
        query = SlotQuery(doctor_id=therapist.id, limit=0)
        slots = self.engine.find_slots(query)
        self.assertTrue(slots)
        today = [slot for slot in slots if slot.start.date() == NOW.date()]
        for slot in today:
            lead = (slot.start - NOW).total_seconds() / 60
            self.assertGreaterEqual(lead, MIN_LEAD_MINUTES)
            self.assertEqual(MIN_LEAD_MINUTES, 30)

    def test_same_day_boundary_just_under_and_over_30(self) -> None:
        """Слот ровно через 30 минут проходит, через 29 — нет."""
        therapist = self.engine.doctors_of_specialty("терапевт")[0]
        days = self.engine.doctor_days(therapist.id)
        weekday = NOW.date().weekday()
        times = days.get(weekday) or days[sorted(days)[0]]
        target = times[len(times) // 2]
        hour, minute = (int(part) for part in target.split(":"))

        def engine_at(moment: datetime) -> SlotEngine:
            return SlotEngine(now_provider=lambda: moment)

        slot_moment = datetime.combine(NOW.date(), time(hour, minute))
        exact = engine_at(slot_moment - timedelta(minutes=30)).find_slots(
            SlotQuery(doctor_id=therapist.id, limit=0)
        )
        self.assertIn(
            slot_moment,
            [slot.start for slot in exact],
            "слот ровно за 30 минут должен быть доступен",
        )
        late = engine_at(slot_moment - timedelta(minutes=29)).find_slots(
            SlotQuery(doctor_id=therapist.id, limit=0)
        )
        self.assertNotIn(
            slot_moment,
            [slot.start for slot in late],
            "слот менее чем за 30 минут предлагать нельзя",
        )

    def test_include_same_day_false(self) -> None:
        slots = self.engine.find_slots(
            SlotQuery(specialty="терапевт", include_same_day=False, limit=0)
        )
        self.assertTrue(slots)
        self.assertTrue(all(slot.start.date() > NOW.date() for slot in slots))

    # --- 5. занятость исключается ---

    def test_busy_tuple_excludes_slot(self) -> None:
        slots = self.engine.find_slots(SlotQuery(specialty="терапевт", limit=0))
        self.assertTrue(slots)
        taken = slots[0]
        busy = [(taken.doctor_id, taken.start)]
        after = self.engine.find_slots(
            SlotQuery(specialty="терапевт", limit=0), busy=busy
        )
        self.assertNotIn((taken.doctor_id, taken.start), [(s.doctor_id, s.start) for s in after])
        self.assertEqual(len(after), len(slots) - 1)

    def test_busy_visit_excludes_slot(self) -> None:
        slots = self.engine.find_slots(
            SlotQuery(doctor_id=self.engine.doctors_of_specialty("терапевт")[0].id, limit=0)
        )
        self.assertTrue(slots)
        taken = slots[0]
        doctor = next(d for d in self.engine.doctors() if d.id == taken.doctor_id)
        visit = Visit(
            id=1,
            client_id=1,
            doctor=doctor,
            scheduled_at=taken.api_text,
            status=VisitStatus.PLANNED.value,
        )
        after = self.engine.find_slots(
            SlotQuery(doctor_id=taken.doctor_id, limit=0), busy=[visit]
        )
        self.assertNotIn(taken.start, [slot.start for slot in after])

    def test_cancelled_visit_does_not_block(self) -> None:
        slots = self.engine.find_slots(
            SlotQuery(doctor_id=self.engine.doctors_of_specialty("терапевт")[0].id, limit=0)
        )
        taken = slots[0]
        doctor = next(d for d in self.engine.doctors() if d.id == taken.doctor_id)
        cancelled = Visit(
            id=2,
            client_id=1,
            doctor=doctor,
            scheduled_at=taken.api_text,
            status=VisitStatus.CANCELLED.value,
        )
        after = self.engine.find_slots(
            SlotQuery(doctor_id=taken.doctor_id, limit=0), busy=[cancelled]
        )
        self.assertIn(taken.start, [slot.start for slot in after])

    def test_busy_other_doctor_does_not_block(self) -> None:
        therapist_id = self.engine.doctors_of_specialty("терапевт")[0].id
        slots = self.engine.find_slots(SlotQuery(doctor_id=therapist_id, limit=0))
        taken = slots[0]
        other_id = next(d.id for d in self.engine.doctors() if d.id != taken.doctor_id)
        after = self.engine.find_slots(
            SlotQuery(doctor_id=therapist_id, limit=0), busy=[(other_id, taken.start)]
        )
        self.assertIn(taken.start, [slot.start for slot in after])

    def test_busy_visit_with_foreign_doctor_id_matches_by_name(self) -> None:
        """Стенд МИС нумерует врачей иначе — сопоставляем по ФИО."""
        slots = self.engine.find_slots(
            SlotQuery(doctor_id=self.engine.doctors_of_specialty("терапевт")[0].id, limit=0)
        )
        taken = slots[0]
        ours = next(d for d in self.engine.doctors() if d.id == taken.doctor_id)
        foreign = Doctor(
            id=ours.id + 5000,
            full_name=ours.full_name,
            specialty=ours.specialty,
        )
        visit = Visit(
            id=7,
            client_id=7,
            doctor=foreign,
            scheduled_at=taken.api_text,
            status=VisitStatus.PLANNED.value,
        )
        after = self.engine.find_slots(
            SlotQuery(doctor_id=taken.doctor_id, limit=0), busy=[visit]
        )
        self.assertNotIn(taken.start, [slot.start for slot in after])

    def test_busy_tuple_by_doctor_name(self) -> None:
        slots = self.engine.find_slots(
            SlotQuery(doctor_id=self.engine.doctors_of_specialty("терапевт")[0].id, limit=0)
        )
        taken = slots[0]
        ours = next(d for d in self.engine.doctors() if d.id == taken.doctor_id)
        after = self.engine.find_slots(
            SlotQuery(doctor_id=taken.doctor_id, limit=0),
            busy=[(ours.full_name, taken.start)],
        )
        self.assertNotIn(taken.start, [slot.start for slot in after])
        short = self.engine.find_slots(
            SlotQuery(doctor_id=taken.doctor_id, limit=0),
            busy=[(ours.short_name, taken.start)],
        )
        self.assertNotIn(taken.start, [slot.start for slot in short])

    # --- 6. limit ---

    def test_default_limit_is_three(self) -> None:
        slots = self.engine.find_slots(SlotQuery(specialty="терапевт"))
        self.assertEqual(len(slots), 3)
        self.assertEqual(SlotQuery().limit, 3)

    def test_explicit_limit(self) -> None:
        one = self.engine.find_slots(SlotQuery(specialty="терапевт", limit=1))
        self.assertEqual(len(one), 1)
        all_slots = self.engine.find_slots(SlotQuery(specialty="терапевт", limit=0))
        self.assertEqual(one[0].start, all_slots[0].start)

    # --- прочее по контракту ---

    def test_slot_payload(self) -> None:
        slot = self.engine.find_slots(SlotQuery(specialty="терапевт", limit=1))[0]
        payload = slot.to_dict()
        self.assertEqual(payload["doctor_id"], slot.doctor_id)
        self.assertEqual(payload["time"], slot.start.strftime("%H:%M"))
        self.assertEqual(payload["date"], slot.start.strftime("%d.%m.%Y"))
        self.assertEqual(payload["start"], slot.start.strftime("%d.%m.%Y %H:%M"))

    def test_unknown_specialty_gives_no_slots(self) -> None:
        self.assertEqual(self.engine.find_slots(SlotQuery(specialty="астролог")), [])
        self.assertIsNone(self.engine.resolve_specialty("астролог"))

    def test_doctors_of_specialty(self) -> None:
        doctors = self.engine.doctors_of_specialty("терапевт")
        self.assertTrue(doctors)
        self.assertTrue(all(d.specialty == "терапевт" for d in doctors))
        # регистр и синоним не ломают поиск
        self.assertEqual(
            [d.id for d in doctors],
            [d.id for d in self.engine.doctors_of_specialty("Терапевт")],
        )
        self.assertEqual(
            [d.id for d in self.engine.doctors_of_specialty("к терапевту")],
            [d.id for d in doctors],
        )

    def test_resolve_specialty_synonyms(self) -> None:
        cases = {
            "лор": "оториноларинголог",
            "ЛОР": "оториноларинголог",
            "к лору": "оториноларинголог",
            "ухо-горло-нос": "оториноларинголог",
            "окулист": "офтальмолог",
            "к окулисту": "офтальмолог",
            "глазное": "офтальмолог",
            "гинеколог": "акушер-гинеколог",
            "к гинекологу": "акушер-гинеколог",
            "сердечный": "кардиолог",
            "к кардиологу": "кардиолог",
            "нервопатолог": "невролог",
            "желудок": "гастроэнтеролог",
            "почки": "уролог",
            "кожа": "дерматовенеролог",
            "травматолог": "травматолог-ортопед",
            "участковый терапевт": "терапевт",
            "детский врач": "педиатр",
            "зуб болит": "стоматолог",
            "терапевт": "терапевт",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.engine.resolve_specialty(text), expected)
        self.assertIsNone(self.engine.resolve_specialty(""))
        self.assertIsNone(self.engine.resolve_specialty("погода на завтра"))

    def test_specialty_filter_matches_doctor_specialty(self) -> None:
        for specialty in {d.specialty for d in self.engine.doctors()}:
            slots = self.engine.find_slots(SlotQuery(specialty=specialty, limit=0))
            self.assertTrue(slots, f"нет слотов по специальности {specialty}")
            self.assertTrue(all(slot.specialty == specialty for slot in slots))

    def test_doctor_id_filter(self) -> None:
        doctor = self.engine.doctors_of_specialty("кардиолог")[0]
        slots = self.engine.find_slots(SlotQuery(doctor_id=doctor.id, limit=0))
        self.assertTrue(slots)
        self.assertTrue(all(slot.doctor_id == doctor.id for slot in slots))

    def test_horizon_can_be_narrowed(self) -> None:
        slots = self.engine.find_slots(
            SlotQuery(specialty="терапевт", date_from=NOW.date() + timedelta(days=3), limit=0)
        )
        self.assertTrue(slots)
        self.assertTrue(all(slot.start.date() >= NOW.date() + timedelta(days=3) for slot in slots))
        # за пределами горизонта слотов нет
        far = self.engine.find_slots(
            SlotQuery(
                specialty="терапевт",
                date_from=NOW.date() + timedelta(days=HORIZON_DAYS + 5),
                limit=0,
            )
        )
        self.assertEqual(far, [])

    def test_waiting_terms(self) -> None:
        terms = self.engine.waiting_terms()
        self.assertEqual(terms["терапевт"], 1)
        self.assertEqual(terms["педиатр"], 1)
        self.assertEqual(terms["кардиолог"], 14)
        self.assertEqual(terms["онкология"], 3)
        self.assertEqual(terms["неотложная помощь"], 0)
        self.assertEqual(set(WAITING_TERMS), set(WAITING_TERMS) & set(terms))
        # справочник отдаётся копией: правки наружу не протекают
        terms["терапевт"] = 999
        self.assertEqual(self.engine.waiting_terms()["терапевт"], 1)

    def test_deterministic_between_calls(self) -> None:
        query = SlotQuery(specialty="терапевт", limit=0)
        first = [(s.doctor_id, s.start, s.end) for s in self.engine.find_slots(query)]
        second = [(s.doctor_id, s.start, s.end) for s in self.engine.find_slots(query, busy=[])]
        self.assertEqual(first, second)
        # два независимых движка с тем же «сейчас» дают то же самое
        other = build_engine()
        self.assertEqual(
            first,
            [(s.doctor_id, s.start, s.end) for s in other.find_slots(query)],
        )


class SlotEngineDegradedTests(unittest.TestCase):
    """Данные не собраны: движок не падает и честно отвечает «не готов»."""

    def setUp(self) -> None:
        missing = str(PROJECT_ROOT / "data" / "__no_such_file__.json")
        self.engine = SlotEngine(
            schedule_path=missing,
            doctors_path=missing,
            specialties_path=missing,
            now_provider=lambda: NOW,
        )

    def test_is_ready_false_and_empty_results(self) -> None:
        self.assertFalse(self.engine.is_ready)
        self.assertEqual(self.engine.doctors(), [])
        self.assertEqual(self.engine.find_slots(SlotQuery(specialty="терапевт")), [])
        self.assertEqual(self.engine.doctor_days(1), {})
        self.assertIsNone(self.engine.doctor_shift(1))
        self.assertEqual(self.engine.doctors_of_specialty("терапевт"), [])
        self.assertTrue(self.engine.load_errors)
        # даже без данных сроки ПГГ известны из регламента
        self.assertEqual(self.engine.waiting_terms()["терапевт"], 1)

    def test_missing_data_does_not_break_construction(self) -> None:
        engine = SlotEngine()
        self.assertIsInstance(engine.is_ready, bool)
        engine.find_slots(SlotQuery())  # не должно бросать исключение

    def test_corrupt_json_is_reported_not_raised(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "doctors.json"
            broken.write_text("{ это не JSON", encoding="utf-8")
            good = Path(tmp) / "schedule_grid.json"
            source = PROJECT_ROOT / "data" / "schedule_grid.json"
            if source.exists():
                good.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                good.write_text(json.dumps({"schedules": []}), encoding="utf-8")
            engine = SlotEngine(
                doctors_path=str(broken),
                schedule_path=str(good),
                specialties_path=str(Path(tmp) / "none.json"),
                now_provider=lambda: NOW,
            )
            self.assertFalse(engine.is_ready)
            self.assertEqual(engine.find_slots(SlotQuery(specialty="терапевт")), [])
            self.assertTrue(any("не читается" in e for e in engine.load_errors))
            self.assertTrue(any("не найден" in e for e in engine.load_errors))


class SlotEngineNowProviderTests(unittest.TestCase):
    """``now_provider`` полностью определяет «сегодня»."""

    def test_now_provider_accepts_date_and_datetime(self) -> None:
        as_date = SlotEngine(
            now_provider=lambda: date(2026, 10, 5),
            schedule_path=str(PROJECT_ROOT / "data" / "__none__.json"),
        )
        self.assertFalse(as_date.is_ready)  # дата не ломает конструктор

    def test_sunday_today_shifts_first_available_day(self) -> None:
        engine = build_engine()
        if not engine.is_ready:
            self.skipTest("data/* не собраны")
        sunday = datetime(2026, 10, 4, 10, 0)  # воскресенье
        engine = SlotEngine(now_provider=lambda: sunday)
        slots = engine.find_slots(SlotQuery(specialty="терапевт", limit=0))
        self.assertTrue(slots)
        self.assertTrue(all(slot.start.date() > sunday.date() for slot in slots))
        self.assertTrue(all(slot.start.weekday() != 6 for slot in slots))


if __name__ == "__main__":
    unittest.main(verbosity=2)

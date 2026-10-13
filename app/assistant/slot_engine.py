"""Слот-движок регистратуры: свободные окна приёма по расписанию врачей.

Контракт — ``docs/ARCHITECTURE.md`` 3.2. Данные читаются из трёх JSON,
которые собирает ``tools/build_schedule.py``:

* ``data/schedule_grid.json`` — сетка приёма по дням недели (``DoctorSchedule``);
* ``data/doctors.json`` — справочник врачей (+ смена, шаг, кабинет);
* ``data/specialties.json`` — каталог специальностей, прямые и по направлению,
  синонимы.

Принципы модуля:

1. **Лениво и без падений.** Конструктор не читает файлы и не бросает
   исключений: если ``data/*`` ещё не собраны, ``is_ready`` = ``False``,
   а все методы возвращают пустой/безопасный результат.
2. **Детерминизм.** Внутри логики нет обращений к системным часам: время
   берётся из ``now_provider`` (callable, возвращающий ``datetime``/``date``)
   или из ``datetime.now()``, если провайдер не задан. При одном и том же
   ``now`` и одних данных результат ``find_slots`` всегда одинаков.
3. **Данные МИС — источник истины.** Слоты не выдумываются: они берутся из
   сетки врача и обрезаются часами работы поликлиники (регламент 2.2:
   будни 07:30–20:00, суббота 09:00–15:00, воскресенье — выходной).

Правила выдачи слотов (регламент 5, С-01):

* горизонт — 14 календарных дней от «сегодня»;
* сегодняшний слот доступен, если до него не меньше 30 минут;
* ``part_of_day``: ``morning`` — до 12:00, ``afternoon`` — 12:00–17:00,
  ``evening`` — с 17:00 (``time_from``/``time_to`` уточняют точнее);
* ``weekdays`` — белый список дней недели (0 — понедельник), если задан;
* ``exclude_weekdays`` — чёрный список («только не в пятницу»);
* занятые моменты исключаются: ``busy`` принимает ``list[tuple[int, datetime]]``
  (врач, начало) и ``list[Visit]``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from app.types import (
    DATETIME_FMT,
    Doctor,
    DoctorSchedule,
    Slot,
    SlotQuery,
    VisitStatus,
)

__all__ = ["SlotEngine", "CLINIC_HOURS", "MIN_LEAD_MINUTES", "WAITING_TERMS"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Часы работы поликлиники, регламент 2.2. ``None`` — выходной.
CLINIC_HOURS: dict[int, Optional[tuple[str, str]]] = {
    0: ("07:30", "20:00"),
    1: ("07:30", "20:00"),
    2: ("07:30", "20:00"),
    3: ("07:30", "20:00"),
    4: ("07:30", "20:00"),
    5: ("09:00", "15:00"),
    6: None,  # воскресенье — выходной
}

#: Минимальный запас до слота «сегодня» (регламент 5, С-01).
MIN_LEAD_MINUTES = 30

#: Границы частей дня для ``SlotQuery.part_of_day`` (нижняя включительно).
PART_OF_DAY: dict[str, tuple[int, int]] = {
    "morning": (0, 12 * 60),
    "afternoon": (12 * 60, 17 * 60),
    "evening": (17 * 60, 24 * 60),
}

#: Сроки ожидания по ПГГ, рабочих дней (регламент 2.4). Для специальностей,
#: которых нет в словаре (например, появившихся в новой выгрузке), действует
#: общий срок плановой консультации специалиста — 14 рабочих дней.
WAITING_TERMS: dict[str, int] = {
    # первичная помощь — не более 24 часов
    "терапевт": 1,
    "педиатр": 1,
    "врач общей практики": 1,
    # неотложная помощь — не более 2 часов
    "неотложная помощь": 0,
    # плановая консультация специалиста — не более 14 рабочих дней
    "кардиолог": 14,
    "невролог": 14,
    "эндокринолог": 14,
    "гастроэнтеролог": 14,
    "уролог": 14,
    "пульмонолог": 14,
    "хирург": 14,
    "офтальмолог": 14,
    "оториноларинголог": 14,
    "акушер-гинеколог": 14,
    "дерматовенеролог": 14,
    "травматолог-ортопед": 14,
    "стоматолог": 14,
    # подозрение на онкологическое заболевание — не более 3 рабочих дней
    "онкология": 3,
    "онкологическое заболевание": 3,
    # плановая диагностика — не более 14 рабочих дней
    "диагностика": 14,
}

_DEFAULT_WAITING_TERM = 14

#: Падежные окончания для коротких синонимов («лор» → «лору», «ухо» → «уха»).
_CASE_ENDINGS = ("", "а", "у", "е", "ом", "ем", "ы", "и", "ю", "ей", "ой")

#: Минимальный каталог специальностей, если ``specialties.json`` не собран.
_FALLBACK_SPECIALTIES: dict[str, Any] = {
    "default": "терапевт",
    "primary_care": ["терапевт", "педиатр"],
    "referral_required": [
        "кардиолог",
        "невролог",
        "эндокринолог",
        "гастроэнтеролог",
        "уролог",
        "пульмонолог",
    ],
    "direct": [
        "терапевт",
        "педиатр",
        "хирург",
        "офтальмолог",
        "оториноларинголог",
        "акушер-гинеколог",
        "дерматовенеролог",
        "травматолог-ортопед",
    ],
    "synonyms": {},
}


def _norm(text: Any) -> str:
    """Нормализация текста: регистр, ё/е, пробелы, кавычки и дефисы-разделители."""
    if text is None:
        return ""
    value = str(text).lower().replace("ё", "е")
    value = value.replace("–", "-").replace("—", "-").replace("_", " ")
    for ch in "\"'«»,.!?;:()[]":
        value = value.replace(ch, " ")
    return " ".join(value.split())


def _norm_specialty(text: Any) -> str:
    return _norm(text)


def _norm_name(text: Any) -> str:
    """Нормализация ФИО для сопоставления со справочником стенда."""
    return _norm(text)


def _parse_hhmm(value: Any) -> Optional[time]:
    """«08:45» / «8:45» → ``time``; мусор → ``None``."""
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    raw = str(value or "").strip().replace(".", ":").replace("-", ":")
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _minutes_of(value: Any) -> Optional[int]:
    parsed = _parse_hhmm(value)
    if parsed is None:
        return None
    return parsed.hour * 60 + parsed.minute


def _as_datetime(value: Any) -> Optional[datetime]:
    """Приводит момент к ``datetime``: datetime/date/строка стенда/ISO."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0))
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        for fmt in (
            DATETIME_FMT,
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return None


def _status_value(status: Any) -> str:
    return str(getattr(status, "value", status) or "")


class SlotEngine:
    """Свободные слоты приёма по расписанию, собранному из истории записей."""

    def __init__(
        self,
        *,
        schedule_path: str = "data/schedule_grid.json",
        doctors_path: str = "data/doctors.json",
        specialties_path: str = "data/specialties.json",
        horizon_days: int = 14,
        now_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.schedule_path = schedule_path
        self.doctors_path = doctors_path
        self.specialties_path = specialties_path
        self.horizon_days = int(horizon_days) if horizon_days else 14
        #: явный горизонт из аргумента важнее значения в файле расписания
        self._horizon_from_arg = int(horizon_days or 0) != 14
        self._now_provider = now_provider

        self._loaded = False
        self._doctors: list[Doctor] = []
        self._doctor_meta: dict[int, dict[str, Any]] = {}
        self._schedules: dict[int, DoctorSchedule] = {}
        self._specialties: dict[str, Any] = dict(_FALLBACK_SPECIALTIES)

        #: Человекочитаемые причины, по которым данные не загрузились
        #: (для ``/api/health`` и панели демо). Контракт 3.2 их не требует.
        self.load_errors: list[str] = []
        #: Снимок данных до ``align_with_stand`` — чтобы сопоставление со
        #: справочником реального стенда не портило исходное расписание.
        self._stand_snapshot: Optional[tuple] = None
        #: True, если справочник уже сопоставлен с реальным стендом.
        self.aligned_with_stand: bool = False

    # ---------------------------------------------------------------- данные

    def _resolve_path(self, raw: Any) -> Path:
        path = Path(str(raw))
        if path.is_absolute():
            return path
        cwd_candidate = Path.cwd() / path
        if cwd_candidate.exists():
            return cwd_candidate
        return PROJECT_ROOT / path

    def _read_json(self, raw_path: Any, label: str) -> Optional[dict]:
        path = self._resolve_path(raw_path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            self.load_errors.append(f"{label}: файл не найден ({path})")
            return None
        except (OSError, ValueError) as exc:
            self.load_errors.append(f"{label}: не читается ({path}): {exc}")
            return None
        if not isinstance(payload, dict):
            self.load_errors.append(f"{label}: ожидался объект JSON ({path})")
            return None
        return payload

    def _ensure_loaded(self) -> None:
        """Ленивая загрузка. Никогда не бросает исключение наружу."""
        if self._loaded:
            return
        self._loaded = True

        doctors_payload = self._read_json(self.doctors_path, "doctors")
        if doctors_payload:
            for row in doctors_payload.get("doctors") or []:
                try:
                    doctor = Doctor.from_api(row)
                except (TypeError, ValueError, KeyError) as exc:
                    self.load_errors.append(f"doctors: строка пропущена: {exc}")
                    continue
                self._doctors.append(doctor)
                shift = row.get("shift") or {}
                start = str(shift.get("start") or "").strip()
                end = str(shift.get("end") or "").strip()
                self._doctor_meta[doctor.id] = {
                    "shift": (start, end) if start and end else None,
                    "step_minutes": int(row.get("step_minutes") or 0) or None,
                    "cabinet": (
                        str(row["cabinet"]) if row.get("cabinet") not in (None, "") else None
                    ),
                    "weekdays": tuple(int(w) for w in (row.get("weekdays") or ())),
                }
        self._doctors.sort(key=lambda d: d.id)

        schedule_payload = self._read_json(self.schedule_path, "schedule_grid")
        if schedule_payload:
            for row in schedule_payload.get("schedules") or []:
                try:
                    schedule = DoctorSchedule.from_dict(row)
                except (TypeError, ValueError, KeyError) as exc:
                    self.load_errors.append(f"schedule_grid: строка пропущена: {exc}")
                    continue
                self._schedules[schedule.doctor_id] = schedule
            horizon = schedule_payload.get("horizon_days")
            if horizon and not self._horizon_from_arg:
                try:
                    self.horizon_days = int(horizon)
                except (TypeError, ValueError):
                    pass

        specialties_payload = self._read_json(self.specialties_path, "specialties")
        if specialties_payload:
            merged = dict(_FALLBACK_SPECIALTIES)
            merged.update(specialties_payload)
            self._specialties = merged

    # ------------------------------------------------------------ контракт

    @property
    def is_ready(self) -> bool:
        """``True``, если справочник врачей и сетка расписания загружены."""
        self._ensure_loaded()
        return bool(self._doctors) and bool(self._schedules)

    def doctors(self) -> list[Doctor]:
        """Справочник врачей (включая неактивных), по возрастанию ``id``."""
        self._ensure_loaded()
        return list(self._doctors)

    def align_with_stand(self, stand_doctors: "list[Doctor]") -> dict:
        """Сопоставить расписание со справочником реального стенда.

        Зачем: ``data/*.json`` собраны из истории записей и содержат свои
        идентификаторы врачей. На реальном стенде справочник приходит из
        ``GET /doctors`` со своими id, и создавать записи нужно именно с ними —
        иначе стенд молча запишет пациента к другому врачу.

        Сопоставление идёт по ФИО, а если врача из истории на стенде нет — по
        специальности. Сетка приёма не меняется: расписание остаётся нашей
        ответственностью (стенд его не хранит).

        Возвращает отчёт ``{"mapped", "by_name", "by_specialty", "unmapped"}``.
        Если совпадений нет вовсе, данные движка не трогаются.
        """
        self._ensure_loaded()
        if not stand_doctors:
            return {"mapped": 0, "by_name": 0, "by_specialty": 0, "unmapped": 0}

        original_doctors = list(self._doctors)
        original_meta = dict(self._doctor_meta)
        original_schedules = dict(self._schedules)

        by_name: dict[str, Doctor] = {}
        for doctor in original_doctors:
            by_name.setdefault(_norm_name(doctor.full_name), doctor)

        new_doctors: list[Doctor] = []
        new_meta: dict[int, dict] = {}
        new_schedules: dict[int, DoctorSchedule] = {}
        used_sources: set[int] = set()
        stats = {"mapped": 0, "by_name": 0, "by_specialty": 0, "unmapped": 0}

        for stand_doctor in sorted(stand_doctors, key=lambda d: d.id):
            new_doctors.append(stand_doctor)
            source = by_name.get(_norm_name(stand_doctor.full_name))
            how = ""
            if source is not None and source.id in original_schedules:
                how = "by_name"
            else:
                for candidate in original_doctors:
                    if (candidate.id not in used_sources
                            and candidate.id in original_schedules
                            and _norm_specialty(candidate.specialty)
                            == _norm_specialty(stand_doctor.specialty)):
                        source = candidate
                        how = "by_specialty"
                        break
            if source is None or how == "":
                stats["unmapped"] += 1
                continue

            used_sources.add(source.id)
            schedule = original_schedules[source.id]
            new_schedules[stand_doctor.id] = DoctorSchedule(
                doctor_id=stand_doctor.id,
                full_name=stand_doctor.full_name,
                specialty=stand_doctor.specialty,
                step_minutes=schedule.step_minutes,
                slots_by_weekday=dict(schedule.slots_by_weekday),
            )
            meta = original_meta.get(source.id)
            if meta:
                new_meta[stand_doctor.id] = dict(meta)
            stats["mapped"] += 1
            stats[how] += 1

        if not stats["mapped"]:
            return stats

        self._doctors = new_doctors
        self._doctor_meta = new_meta
        self._schedules = new_schedules
        self._stand_snapshot = (original_doctors, original_meta, original_schedules)
        self.aligned_with_stand = True
        return stats

    def doctor_days(self, doctor_id: int) -> dict[int, tuple[str, ...]]:
        """Времена начала приёма по дням недели: ``{0: ("08:00", ...), ...}``."""
        self._ensure_loaded()
        schedule = self._schedule_of(doctor_id)
        if schedule is None:
            return {}
        return {int(wd): tuple(times) for wd, times in schedule.slots_by_weekday.items()}

    def doctor_shift(self, doctor_id: int) -> Optional[tuple[str, str]]:
        """Границы смены врача ``("08:00", "14:30")`` или ``None``."""
        self._ensure_loaded()
        doctor_id = self._as_id(doctor_id)
        if doctor_id is None:
            return None
        meta = self._doctor_meta.get(doctor_id) or {}
        if meta.get("shift"):
            return tuple(meta["shift"])  # type: ignore[return-value]
        schedule = self._schedules.get(doctor_id)
        if schedule is None:
            return None
        starts: list[str] = []
        ends: list[str] = []
        for wd, times in schedule.slots_by_weekday.items():
            if not times:
                continue
            starts.append(times[0])
            end_minutes = _minutes_of(times[-1])
            if end_minutes is not None:
                end_minutes += schedule.step_minutes
                ends.append(f"{end_minutes // 60:02d}:{end_minutes % 60:02d}")
        if not starts or not ends:
            return None
        return (min(starts), max(ends))

    def find_slots(self, query: SlotQuery, *, busy: Optional[Iterable[Any]] = None) -> list[Slot]:
        """Свободные слоты по запросу ``SlotQuery``.

        ``busy`` — занятые моменты: ``list[tuple[int, datetime]]`` (врач, начало)
        или ``list[Visit]``. Отменённые записи слот не занимают.
        """
        self._ensure_loaded()
        if not self.is_ready:
            return []
        if query is None:
            query = SlotQuery()

        now = self._now()
        today = now.date()
        horizon_end = today + timedelta(days=max(1, self.horizon_days) - 1)

        first_day = query.date_from or today
        last_day = query.date_to or horizon_end
        if first_day < today:
            first_day = today
        if last_day > horizon_end:
            last_day = horizon_end
        if last_day < first_day:
            return []

        candidates = self._candidate_doctors(query)
        if not candidates:
            return []

        busy_index = self._busy_index(busy)
        exclude_weekdays = tuple(query.exclude_weekdays or ())
        allowed_weekdays = tuple(query.weekdays) if query.weekdays is not None else None
        limit = query.limit
        try:
            limit = int(limit) if limit is not None else 3
        except (TypeError, ValueError):
            limit = 3

        slots: list[Slot] = []
        day = first_day
        while day <= last_day:
            weekday = day.weekday()
            window = CLINIC_HOURS.get(weekday)
            if window is None:
                day += timedelta(days=1)
                continue
            if allowed_weekdays is not None and weekday not in allowed_weekdays:
                day += timedelta(days=1)
                continue
            if weekday in exclude_weekdays:
                day += timedelta(days=1)
                continue

            open_minutes, close_minutes = _minutes_of(window[0]), _minutes_of(window[1])
            is_today = day == today
            for doctor in candidates:
                schedule = self._schedules.get(doctor.id)
                if schedule is None:
                    continue
                step = int(
                    (self._doctor_meta.get(doctor.id) or {}).get("step_minutes")
                    or schedule.step_minutes
                    or 20
                )
                for text in schedule.slots(weekday):
                    start_time = _parse_hhmm(text)
                    if start_time is None:
                        continue
                    start = datetime.combine(day, start_time)
                    end = start + timedelta(minutes=step)
                    if open_minutes is not None and start_time.hour * 60 + start_time.minute < open_minutes:
                        continue
                    if close_minutes is not None and end.hour * 60 + end.minute > close_minutes:
                        continue
                    if is_today:
                        if not query.include_same_day:
                            continue
                        if start - now < timedelta(minutes=MIN_LEAD_MINUTES):
                            continue
                    if not self._match_filters(start, query):
                        continue
                    if any(start <= moment < end for moment in busy_index.get(doctor.id, ())):
                        continue
                    slots.append(
                        Slot(
                            doctor_id=doctor.id,
                            doctor_name=doctor.full_name,
                            specialty=doctor.specialty,
                            start=start,
                            end=end,
                            cabinet=(self._doctor_meta.get(doctor.id) or {}).get("cabinet"),
                        )
                    )
            day += timedelta(days=1)

        slots.sort(key=lambda slot: (slot.start, slot.doctor_id))
        if limit and limit > 0:
            slots = slots[:limit]
        return slots

    def resolve_specialty(self, text: str) -> Optional[str]:
        """«к лору» → ``"оториноларинголог"``. Неизвестное → ``None``."""
        self._ensure_loaded()
        needle = _norm(text)
        if not needle:
            return None

        catalog = self._specialty_catalog()
        if needle in catalog:
            return needle
        compact = needle.replace("-", " ")
        for name in catalog:
            if name.replace("-", " ") == compact:
                return name

        aliases = self._specialty_aliases()
        # 1) точное совпадение по нормализованному синониму
        for canonical, variants in aliases:
            for variant in variants:
                if needle == variant:
                    return canonical
        # 2) многословные синонимы внутри фразы («болит ухо горло нос»)
        for canonical, variants in aliases:
            for variant in variants:
                if " " in variant and variant in needle:
                    return canonical
        # 3) словоформы: «кардиологу», «окулиста», «лору» — сравниваем основы
        words = needle.replace("-", " ").split()
        for canonical, variants in aliases:
            for variant in variants:
                if " " in variant:
                    continue
                if len(variant) >= 4:
                    stem = variant[:-2] if len(variant) >= 6 else variant[:-1]
                    if any(word.startswith(stem) for word in words):
                        return canonical
                else:
                    # короткие синонимы («лор», «ухо», «нос») — только словоформы
                    # с падежными окончаниями, чтобы «вопрос» не стал «воп»
                    forms = {variant + ending for ending in _CASE_ENDINGS}
                    if any(word in forms for word in words):
                        return canonical
        return None

    def doctors_of_specialty(self, specialty: str) -> list[Doctor]:
        """Врачи специальности (включая неактивных), по возрастанию ``id``."""
        self._ensure_loaded()
        target = _norm_specialty(specialty)
        if not target:
            return []
        resolved = self.resolve_specialty(target) or target
        result = [
            doctor
            for doctor in self._doctors
            if _norm_specialty(doctor.specialty) == resolved
        ]
        return result

    def waiting_terms(self) -> dict[str, int]:
        """Сроки ожидания по ПГГ в рабочих днях (регламент 2.4).

        Ключи — специальности и виды помощи; для всех прочих специальностей
        из текущего справочника действует общий срок 14 рабочих дней.
        """
        self._ensure_loaded()
        terms = dict(WAITING_TERMS)
        for doctor in self._doctors:
            terms.setdefault(_norm_specialty(doctor.specialty), _DEFAULT_WAITING_TERM)
        return terms

    # ------------------------------------------------------------ служебное

    def _now(self) -> datetime:
        if self._now_provider is None:
            return datetime.now()
        value = self._now_provider() if callable(self._now_provider) else self._now_provider
        if isinstance(value, datetime):
            return value
        return _as_datetime(value) or datetime.now()

    def _as_id(self, doctor_id: Any) -> Optional[int]:
        try:
            return int(doctor_id)
        except (TypeError, ValueError):
            return None

    def _schedule_of(self, doctor_id: Any) -> Optional[DoctorSchedule]:
        resolved = self._as_id(doctor_id)
        if resolved is None:
            return None
        return self._schedules.get(resolved)

    def _candidate_doctors(self, query: SlotQuery) -> list[Doctor]:
        if query.doctor_id is not None:
            resolved = self._as_id(query.doctor_id)
            return [d for d in self._doctors if d.id == resolved and d.is_active]
        if query.specialty:
            target = self.resolve_specialty(query.specialty) or _norm_specialty(query.specialty)
            return [d for d in self.doctors_of_specialty(target) if d.is_active]
        return [d for d in self._doctors if d.is_active and d.id in self._schedules]

    def _match_filters(self, start: datetime, query: SlotQuery) -> bool:
        minute = start.hour * 60 + start.minute
        part = _norm(query.part_of_day)
        if part:
            window = PART_OF_DAY.get(part)
            if window is None:
                return False
            if not (window[0] <= minute < window[1]):
                return False
        time_from = _minutes_of(query.time_from) if query.time_from else None
        if time_from is not None and minute < time_from:
            return False
        time_to = _minutes_of(query.time_to) if query.time_to else None
        if time_to is not None and minute > time_to:
            return False
        return True

    def _busy_index(self, busy: Optional[Iterable[Any]]) -> dict[int, list[datetime]]:
        """``busy`` → ``{doctor_id: [datetime, ...]}`` с учётом статуса записей.

        Идентификаторы врача из стенда МИС могут не совпадать с нашими: в этом
        случае врач ищется по ФИО (``Visit.doctor.full_name``), а для кортежа
        допускается ``("Кузнецова Анна Сергеевна", datetime)`` вместо ``id``.
        """
        index: dict[int, list[datetime]] = {}
        if not busy:
            return index
        for item in busy:
            doctor_id: Any = None
            doctor_name: Any = None
            moment: Any = None
            active = True
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                doctor_id, moment = item[0], item[1]
                if isinstance(doctor_id, str) and not doctor_id.strip().isdigit():
                    doctor_name, doctor_id = doctor_id, None
            elif isinstance(item, dict):
                status = item.get("status")
                if status is not None:
                    active = _status_value(status) == VisitStatus.PLANNED.value
                doctor_id = item.get("doctor_id")
                doctor = item.get("doctor")
                if isinstance(doctor, dict):
                    doctor_id = doctor_id if doctor_id is not None else doctor.get("id")
                    doctor_name = doctor.get("full_name") or item.get("doctor_name")
                else:
                    doctor_name = item.get("doctor_name")
                moment = item.get("start") or item.get("scheduled_at")
            else:
                status = getattr(item, "status", None)
                if status is not None:
                    active = _status_value(status) == VisitStatus.PLANNED.value
                doctor = getattr(item, "doctor", None)
                doctor_id = getattr(item, "doctor_id", None)
                if doctor_id is None:
                    doctor_id = getattr(doctor, "id", None)
                doctor_name = (
                    getattr(doctor, "full_name", None)
                    if doctor is not None
                    else getattr(item, "doctor_name", None)
                )
                moment = getattr(item, "start", None) or getattr(item, "scheduled_at", None)
            if not active:
                continue
            resolved_moment = _as_datetime(moment)
            resolved_id = self._resolve_busy_doctor(doctor_id, doctor_name)
            if resolved_id is None or resolved_moment is None:
                continue
            index.setdefault(resolved_id, []).append(resolved_moment)
        return index

    def _resolve_busy_doctor(self, doctor_id: Any, doctor_name: Any = None) -> Optional[int]:
        """Id занятого врача в нашем справочнике: сначала по ФИО, затем по id."""
        if doctor_name:
            by_name = self._doctor_id_by_name(doctor_name)
            if by_name is not None:
                return by_name
        return self._as_id(doctor_id)

    def _doctor_id_by_name(self, full_name: Any) -> Optional[int]:
        """Поиск врача по ФИО: полное совпадение, затем «Фамилия И. О.»."""
        needle = _norm(full_name)
        if not needle:
            return None
        for doctor in self._doctors:
            if _norm(doctor.full_name) == needle:
                return doctor.id
        for doctor in self._doctors:
            if _norm(doctor.short_name) == needle:
                return doctor.id
        surname = needle.split()[0]
        matches = [d for d in self._doctors if _norm(d.surname) == surname]
        if len(matches) == 1:
            return matches[0].id
        return None

    def _specialty_catalog(self) -> list[str]:
        """Канонические названия специальностей (из данных и справочника)."""
        names: set[str] = set()
        for key in ("all", "primary_care", "referral_required", "direct"):
            for value in self._specialties.get(key) or ():
                names.add(_norm_specialty(value))
        for canonical in (self._specialties.get("synonyms") or {}):
            names.add(_norm_specialty(canonical))
        for doctor in self._doctors:
            names.add(_norm_specialty(doctor.specialty))
        names.discard("")
        return sorted(names)

    def _specialty_aliases(self) -> list[tuple[str, list[str]]]:
        """Синонимы специальностей: ``[(канон, [варианты...]), ...]``.

        Сначала длинные варианты — чтобы «ухо-горло-нос» не перебивалось «ухо».
        """
        pairs: list[tuple[str, list[str]]] = []
        synonyms = self._specialties.get("synonyms") or {}
        for canonical, variants in synonyms.items():
            name = _norm_specialty(canonical)
            normalized: list[str] = [name]
            for variant in variants or ():
                text = _norm(variant)
                if text and text not in normalized:
                    normalized.append(text)
            normalized.sort(key=len, reverse=True)
            pairs.append((name, normalized))
        pairs.sort(key=lambda pair: max((len(v) for v in pair[1]), default=0), reverse=True)
        return pairs

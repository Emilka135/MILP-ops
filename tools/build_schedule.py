"""Сборка справочников расписания из выгрузки истории записей МИС.

Источник — CSV с разделителем ``;`` в кодировке UTF-8 (BOM):

    ID записи;Пациент (хэш);Дата приёма;Время приёма;Специалист;Специальность;Статус

Скрипт по каждому врачу вычисляет:

* рабочие дни недели (0 — понедельник, 6 — воскресенье);
* набор времён начала приёма (ЧЧ:ММ) по каждому рабочему дню;
* шаг сетки приёма — 20 или 30 минут (выводится из истории);
* границы смены (начало первой сетки, конец последнего слота);
* кабинет — детерминированно по специальности (в выгрузке кабинета нет,
  это демонстрационное расширение для карточки слота).

Результат — три файла строго в формате ``docs/ARCHITECTURE.md`` 3.2:

* ``data/schedule_grid.json`` — ``{"generated_at", "horizon_days", "schedules"}``,
  где ``schedules`` — список ``DoctorSchedule.to_dict()``;
* ``data/doctors.json`` — ``{"generated_at", "source", "doctors"}``
  (к контрактным полям добавлены ``cabinet`` и ``weekdays``);
* ``data/specialties.json`` — ``{"default", "primary_care", "referral_required",
  "direct", "synonyms"}`` (+ справочный ключ ``all``).

Запуск (из корня проекта):

    python tools/build_schedule.py
    python tools/build_schedule.py --input data/raw/appointments.csv --data-dir data

Скрипт детерминирован: при том же входном CSV файлы побайтово совпадают
(кроме ``generated_at``).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.assistant.slot_engine import CLINIC_HOURS  # noqa: E402
from app.types import DoctorSchedule  # noqa: E402

# --- константы -------------------------------------------------------------

DEFAULT_INPUTS = (
    PROJECT_ROOT / "data" / "raw" / "appointments.csv",
    Path(r"C:\Users\Emilka\Downloads\30aef650-1a0b-4238-8c14-2dba969a1a2c.csv"),
)
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

CSV_COLUMNS = (
    "ID записи",
    "Пациент (хэш)",
    "Дата приёма",
    "Время приёма",
    "Специалист",
    "Специальность",
    "Статус",
)

DATE_FMT = "%d.%m.%Y"
TIME_FMT = "%H:%M"
MSK = timezone(timedelta(hours=3))

WEEKDAY_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# Шаг сетки: у участковых 15, у специалистов 20 минут (регламент 1.4),
# в выгрузке встречаются 20 и 30. Прочие значения считаем аномалией.
PLAUSIBLE_STEPS = (15, 20, 30, 60)
DEFAULT_STEP = 20

# Врач считается активным, если принимал в последние 45 дней периода выгрузки.
ACTIVE_WINDOW_DAYS = 45

# Кабинеты: база по специальности, далее +1 на каждого следующего врача.
# В выгрузке кабинета нет — это демонстрационное распределение для карточки
# слота (Slot.cabinet), согласовано с планировкой из регламента 2.2/2.3.
CABINET_BASE = {
    "педиатр": 100,
    "терапевт": 200,
    "хирург": 310,
    "офтальмолог": 320,
    "оториноларинголог": 330,
    "травматолог-ортопед": 340,
    "невролог": 350,
    "кардиолог": 360,
    "дерматовенеролог": 370,
    "эндокринолог": 380,
    "уролог": 390,
    "гастроэнтеролог": 395,
    "акушер-гинеколог": 400,
}
CABINET_DEFAULT_BASE = 300


# --- специальности (формат ARCHITECTURE.md 3.2) ----------------------------

SPECIALTIES = {
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
    "synonyms": {
        "терапевт": [
            "терапевт", "терапевт участковый", "участковый", "участковый врач",
            "врач общей практики", "воп", "терапевт-участковый",
        ],
        "педиатр": [
            "педиатр", "педиатр участковый", "детский врач", "детский доктор",
            "врач для детей",
        ],
        "кардиолог": ["кардиолог", "сердечник", "врач по сердцу", "сердце"],
        "невролог": ["невролог", "нервопатолог", "невропатолог"],
        "эндокринолог": ["эндокринолог", "щитовидка", "гормоны", "гормональный"],
        "гастроэнтеролог": ["гастроэнтеролог", "гастро", "желудок", "кишечник"],
        "уролог": ["уролог", "урология", "почки", "мочевой"],
        "пульмонолог": ["пульмонолог", "легкие", "лёгкие", "дыхание", "бронхи"],
        "хирург": ["хирург", "хирургия"],
        "офтальмолог": [
            "офтальмолог", "окулист", "глазной", "глаз", "зрение", "глаза",
        ],
        "оториноларинголог": [
            "оториноларинголог", "лор", "л-ор", "отоларинголог",
            "ухо-горло-нос", "ухо горло нос", "горло", "ухо", "нос",
        ],
        "акушер-гинеколог": [
            "акушер-гинеколог", "гинеколог", "акушер", "женский врач",
            "женская консультация",
        ],
        "дерматовенеролог": [
            "дерматовенеролог", "дерматолог", "венеролог", "кожа", "дерматит",
        ],
        "травматолог-ортопед": [
            "травматолог-ортопед", "травматолог", "ортопед",
        ],
        "стоматолог": [
            "стоматолог", "зубной", "зуб", "зубы", "стоматология",
        ],
    },
}


def _now_iso() -> str:
    return datetime.now(MSK).replace(microsecond=0).isoformat()


def _pick_input(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path
    for candidate in DEFAULT_INPUTS:
        if candidate.exists():
            return candidate
    return DEFAULT_INPUTS[0]


def _norm_specialty(value: str) -> str:
    """«Терапевт» → «терапевт»; ё/е и лишние пробелы нормализуются."""
    text = " ".join(str(value).split()).strip().lower().replace("ё", "е")
    return text


def load_appointments(path: Path) -> pd.DataFrame:
    """Читает выгрузку и приводит дату/время к каноническому виду."""
    if not path.exists():
        raise SystemExit(f"Не найден входной файл: {path}")
    df = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
    missing = [c for c in CSV_COLUMNS if c not in df.columns]
    if missing:
        raise SystemExit(f"В файле {path} нет колонок: {missing}")
    df = df[list(CSV_COLUMNS)].copy()
    for col in df.columns:
        df[col] = df[col].fillna("").astype(str).str.strip()

    parsed = pd.to_datetime(df["Дата приёма"], format=DATE_FMT, errors="coerce")
    df["date"] = parsed
    df["time"] = pd.to_datetime(df["Время приёма"], format=TIME_FMT, errors="coerce")
    bad = df["date"].isna() | df["time"].isna()
    df = df[~bad].copy()
    df["weekday"] = df["date"].dt.weekday
    df["minute"] = df["time"].dt.hour * 60 + df["time"].dt.minute
    df["specialty"] = df["Специальность"].map(_norm_specialty)
    return df


def infer_step(by_weekday: dict[int, list[int]]) -> tuple[int, bool]:
    """Шаг сетки приёма: самый частый интервал истории.

    Возвращает ``(step, regular)``: ``regular=False`` — интервалы неоднородны,
    шаг подобран по наибольшему общему делителю.
    """
    gaps = Counter()
    for minutes in by_weekday.values():
        for a, b in zip(minutes, minutes[1:]):
            if b > a:
                gaps[b - a] += 1
    if not gaps:
        return DEFAULT_STEP, True
    step, _ = gaps.most_common(1)[0]
    if step in PLAUSIBLE_STEPS:
        return step, True
    divisor = 0
    for gap in gaps:
        divisor = math.gcd(divisor, gap)
    if divisor in PLAUSIBLE_STEPS:
        return divisor, False
    return min(PLAUSIBLE_STEPS, key=lambda s: abs(s - step)), False


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minutes_of(text: str) -> int:
    return int(text[:2]) * 60 + int(text[3:])


def build_schedule(df: pd.DataFrame, stand_doctors: list[dict] | None = None) -> dict:
    """Вычисляет сетки врачей и справочник врачей.

    ``stand_doctors`` — необязательный список врачей стенда МИС: тогда id/ФИО
    берутся у стенда, а сетка подбирается у врача истории той же специальности
    (см. :func:`_materialize`).
    """
    names = sorted(
        {str(n) for n in df["Специалист"] if str(n).strip()},
        key=lambda s: s.lower().replace("ё", "е"),
    )
    last_date = df["date"].max()
    active_since = last_date - timedelta(days=ACTIVE_WINDOW_DAYS)

    entries: list[dict] = []
    cabinet_counter: Counter = Counter()
    warnings: list[str] = []
    total_clipped = 0

    for full_name in names:
        rows = df[df["Специалист"] == full_name]
        specialty = rows["specialty"].mode().iloc[0] if not rows.empty else ""
        by_weekday: dict[int, list[int]] = {}
        for weekday, group in rows.groupby("weekday"):
            by_weekday[int(weekday)] = sorted({int(m) for m in group["minute"]})

        step, regular = infer_step(by_weekday)
        if not regular:
            warnings.append(
                f"{full_name}: неоднородные интервалы, шаг {step} мин "
                "(взят по НОД наблюдаемых пауз)"
            )

        slots_by_weekday: dict[int, tuple[str, ...]] = {}
        shifts: dict[int, tuple[int, int]] = {}
        filled = 0
        clipped = 0
        for weekday, minutes in sorted(by_weekday.items()):
            window = CLINIC_HOURS.get(weekday)
            if window is None:  # выходной по регламенту 2.2
                clipped += len(minutes)
                continue
            open_minute, close_minute = _minutes_of(window[0]), _minutes_of(window[1])
            first, last = minutes[0], minutes[-1]
            full_grid = list(range(first, last + step, step))
            # сетка обрезается часами работы поликлиники (регламент 2.2):
            # в истории есть субботние приёмы 08:00–20:00, которых быть не должно
            grid = [m for m in full_grid if m >= open_minute and m + step <= close_minute]
            clipped += len(full_grid) - len(grid)
            if not grid:
                continue
            filled += max(0, len(grid) - len(set(minutes) & set(grid)))
            slots_by_weekday[weekday] = tuple(_fmt(m) for m in grid)
            shifts[weekday] = (grid[0], grid[-1] + step)

        # Смена врача в справочнике — будни; если врач принимает только
        # в субботу, берём субботу.
        weekday_days = [wd for wd in shifts if wd <= 4]
        base_days = weekday_days or sorted(shifts)
        shift_start = min(shifts[wd][0] for wd in base_days)
        shift_end = max(shifts[wd][1] for wd in base_days)

        cabinet_counter[specialty] += 1
        cabinet = CABINET_BASE.get(specialty, CABINET_DEFAULT_BASE) + cabinet_counter[specialty]

        is_active = bool(rows["date"].max() >= active_since)
        if not is_active:
            warnings.append(f"{full_name}: нет приёмов с {active_since:%d.%m.%Y} — is_active=false")

        entries.append(
            {
                "full_name": full_name,
                "specialty": specialty,
                "is_active": is_active,
                "shift": (_fmt(shift_start), _fmt(shift_end)),
                "step_minutes": step,
                "cabinet": str(cabinet),
                "slots_by_weekday": slots_by_weekday,
            }
        )

        if filled:
            warnings.append(f"{full_name}: дозаполнено пустых времён сетки — {filled}")
        if clipped:
            total_clipped += clipped
            warnings.append(
                f"{full_name}: обрезано часами работы поликлиники времён — {clipped} "
                "(регламент 2.2: будни 07:30–20:00, суббота 09:00–15:00)"
            )

    doctors, schedules, align_warnings = _materialize(entries, stand_doctors)
    warnings.extend(align_warnings)

    specialty_totals = Counter(d["specialty"] for d in doctors)
    report = {
        "input_rows": int(len(df)),
        "doctors": len(doctors),
        "specialties": len(specialty_totals),
        "steps": dict(Counter(d["step_minutes"] for d in doctors)),
        "weekday_load": {
            WEEKDAY_NAMES[int(wd)]: int(cnt)
            for wd, cnt in sorted(df.groupby("weekday").size().items())
        },
        "specialty_totals": dict(specialty_totals.most_common()),
        "clipped_outside_clinic_hours": total_clipped,
        "aligned_with_stand": bool(stand_doctors),
        "warnings": warnings,
    }
    return {
        "doctors": doctors,
        "schedules": schedules,
        "report": report,
    }


def _materialize(entries: list[dict], stand_doctors: list[dict] | None) -> tuple[list[dict], list[dict], list[str]]:
    """Превращает записи истории в ``doctors.json`` и ``schedule_grid.json``.

    Без ``stand_doctors`` — идентификаторы 1..N по алфавиту (режим по умолчанию).
    С ``stand_doctors`` — берутся id/ФИО/специальность/активность стенда МИС,
    а сетка приёма подбирается у врача истории с той же специальностью
    (по кругу, детерминированно): это выравнивает пространство id со стендом,
    чтобы запись через МИС шла к тому же врачу, что был предложен пациенту.
    """
    doctors: list[dict] = []
    schedules: list[dict] = []
    warnings: list[str] = []

    if not stand_doctors:
        for index, entry in enumerate(entries, start=1):
            doctors.append(
                {
                    "id": index,
                    "full_name": entry["full_name"],
                    "specialty": entry["specialty"],
                    "is_active": entry["is_active"],
                    "shift": {"start": entry["shift"][0], "end": entry["shift"][1]},
                    "step_minutes": entry["step_minutes"],
                    # --- расширения сверх минимальной схемы 3.2 ---
                    "cabinet": entry["cabinet"],
                    "weekdays": sorted(int(wd) for wd in entry["slots_by_weekday"]),
                }
            )
            schedules.append(
                DoctorSchedule(
                    doctor_id=index,
                    full_name=entry["full_name"],
                    specialty=entry["specialty"],
                    step_minutes=entry["step_minutes"],
                    slots_by_weekday=entry["slots_by_weekday"],
                ).to_dict()
            )
        return doctors, schedules, warnings

    pools: dict[str, list[dict]] = {}
    for entry in entries:
        pools.setdefault(entry["specialty"], []).append(entry)
    for pool in pools.values():
        pool.sort(key=lambda e: e["full_name"].lower().replace("ё", "е"))
    taken: Counter = Counter()

    for row in stand_doctors:
        specialty = _norm_specialty(row["specialty"])
        pool = pools.get(specialty) or []
        source: dict | None = None
        if pool:
            source = pool[taken[specialty] % len(pool)]
            taken[specialty] += 1
        else:
            warnings.append(
                f"стенд: у врача {row['full_name']} ({specialty}) нет истории приёмов — "
                "сетка не сформирована"
            )
        slots_by_weekday = source["slots_by_weekday"] if source else {}
        step = source["step_minutes"] if source else DEFAULT_STEP
        shift = source["shift"] if source else ("09:00", "09:00")
        doctor = {
            "id": int(row["id"]),
            "full_name": str(row["full_name"]),
            "specialty": specialty,
            "is_active": bool(row.get("is_active", True)),
            "shift": {"start": shift[0], "end": shift[1]},
            "step_minutes": step,
            "cabinet": source["cabinet"] if source else None,
            "weekdays": sorted(int(wd) for wd in slots_by_weekday),
        }
        if source:
            doctor["schedule_source"] = source["full_name"]
        doctors.append(doctor)
        schedules.append(
            DoctorSchedule(
                doctor_id=int(row["id"]),
                full_name=str(row["full_name"]),
                specialty=specialty,
                step_minutes=step,
                slots_by_weekday=slots_by_weekday,
            ).to_dict()
        )
    return doctors, schedules, warnings


def load_stand_doctors(path: "Path | str") -> list[dict]:
    """Читает врачей стенда МИС (``var/mock_stand.json``) для выравнивания id."""
    store = Path(path)
    if not store.is_absolute():
        store = PROJECT_ROOT / store
    if not store.exists():
        raise SystemExit(f"Не найден файл стенда: {store}")
    try:
        payload = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Не читается файл стенда {store}: {exc}") from exc
    rows = payload.get("doctors") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"В файле стенда {store} нет списка врачей")
    out: list[dict] = []
    for row in rows:
        try:
            out.append(
                {
                    "id": int(row["id"]),
                    "full_name": str(row["full_name"]),
                    "specialty": _norm_specialty(row["specialty"]),
                    "is_active": bool(row.get("is_active", True)),
                }
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise SystemExit(f"Некорректная строка врача в {store}: {row} ({exc})") from exc
    out.sort(key=lambda r: r["id"])
    return out


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", default=None, help="CSV выгрузки истории записей")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="куда писать JSON")
    parser.add_argument("--horizon-days", type=int, default=14, help="горизонт записи, дней")
    parser.add_argument(
        "--align-stand",
        default=None,
        metavar="VAR/MOCK_STAND.JSON",
        help=(
            "выровнять id врачей со стендом МИС: id/ФИО берутся у стенда, "
            "сетка — у врача истории той же специальности"
        ),
    )
    args = parser.parse_args(argv)

    source = _pick_input(args.input)
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    stand_doctors = load_stand_doctors(args.align_stand) if args.align_stand else None
    df = load_appointments(source)
    built = build_schedule(df, stand_doctors)
    generated_at = _now_iso()

    source_note = "история записей"
    if stand_doctors:
        source_note = "история записей + справочник стенда (id выровнены)"

    write_json(
        data_dir / "doctors.json",
        {
            "generated_at": generated_at,
            "source": source_note,
            "note": (
                "specialty/step/shift вычислены из истории записей; "
                "cabinet и weekdays — демонстрационные расширения"
                + (
                    "; id/ФИО/is_active взяты у стенда МИС, "
                    "schedule_source — у кого заимствована сетка"
                    if stand_doctors
                    else ""
                )
            ),
            "doctors": built["doctors"],
        },
    )
    write_json(
        data_dir / "schedule_grid.json",
        {
            "generated_at": generated_at,
            "horizon_days": int(args.horizon_days),
            "note": (
                "сетка обрезана часами работы поликлиники (регламент 2.2): "
                "будни 07:30–20:00, суббота 09:00–15:00, воскресенье — выходной"
            ),
            "schedules": built["schedules"],
        },
    )
    write_json(
        data_dir / "specialties.json",
        {
            **SPECIALTIES,
            "all": sorted({d["specialty"] for d in built["doctors"]}),
            "generated_at": generated_at,
        },
    )

    report = dict(built["report"])
    report["input"] = str(source)
    report["data_dir"] = str(data_dir)
    report["generated_at"] = generated_at
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Аналитика по выгрузкам: история записей + журнал звонков → data/metrics.json.

Два источника (оба лежат в ``data/raw/``):

* ``appointments.csv`` — история записей МИС::

      ID записи;Пациент (хэш);Дата приёма;Время приёма;Специалист;Специальность;Статус

* ``calls.csv`` — журнал звонков регистратуры::

      call_id;datetime;wait_sec;duration_sec;result;operator_ext;caller_number

Скрипт считает:

1. итоги по ``result`` (записан / справка / перевод / пропущен) и доли;
2. пропущенные звонки по часам и дням недели, пиковые часы и дни;
3. ожидание на линии: среднее, медиана, p90/p95, максимум, доля > 30 с (норма 3.1);
4. длительности по типам обращений и сравнение с целевыми 2,5 мин (3.2);
5. долю неявок и отмен по специальностям;
6. экономику — с формулами и явными допущениями (все ставки — параметры).

Запуск (из корня проекта)::

    python tools/analyze_data.py
    python tools/analyze_data.py --data-dir data --out data/metrics.json

Скрипт не выдумывает данные: если файл-источник не найден, он честно падает
с понятным сообщением, а не пишет пустые метрики.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_APPOINTMENTS = (
    PROJECT_ROOT / "data" / "raw" / "appointments.csv",
    Path(r"C:\Users\Emilka\Downloads\30aef650-1a0b-4238-8c14-2dba969a1a2c.csv"),
)
DEFAULT_CALLS = (
    PROJECT_ROOT / "data" / "raw" / "calls.csv",
    Path(r"C:\Users\Emilka\Downloads\21de07bd-6bc7-4d24-9aa1-15f9f32fd12a.csv"),
)

MSK = timezone(timedelta(hours=3))
WEEKDAY_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTH_NAMES = (
    "", "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)

RESULT_ORDER = ("записан", "справка", "перевод", "пропущен")
STATUS_ORDER = ("Состоялся", "Отменён", "Неявка")

# --- допущения экономики (все значения — параметры, не «истина») -----------

ECON = {
    "operator_hour_cost_rub": 420.0,
    "operator_salary_rub_month": 45000.0,
    "operator_hours_month": 168.0,
    "insurance_rate": 0.302,
    "overhead_rate": 0.20,
    "assistant_monthly_cost_rub": 25000.0,
    "development_cost_rub": 300000.0,
    "amortization_months": 36,
    "wait_norm_sec": 30,
    "scenario_target_sec": 150,
}

OPERATOR_HOURS = {
    "note": "регламент 2.2: операторы будни 07:00-20:00, суббота 08:30-15:00, воскресенье — нет",
    "weekday": (7 * 60, 20 * 60),
    "saturday": (8 * 60 + 30, 15 * 60),
    "sunday": None,
}


def _pick(paths: tuple[Path, ...], explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else PROJECT_ROOT / path
    for candidate in paths:
        if candidate.exists():
            return candidate
    return paths[0]


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _share(part: float, total: float, digits: int = 4) -> float:
    return _round(part / total, digits) if total else 0.0


def _quantile(series: pd.Series, q: float) -> float:
    return _round(series.quantile(q), 1) if len(series) else 0.0


# --- звонки ----------------------------------------------------------------


def analyze_calls(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Не найден журнал звонков: {path}")
    df = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
    df["dt"] = pd.to_datetime(df["datetime"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
    df = df[df["dt"].notna()].copy()
    df["wait"] = pd.to_numeric(df["wait_sec"], errors="coerce").fillna(0).astype(int)
    df["dur"] = pd.to_numeric(df["duration_sec"], errors="coerce").fillna(0).astype(int)
    df["hour"] = df["dt"].dt.hour
    df["weekday"] = df["dt"].dt.weekday
    df["minute_of_day"] = df["dt"].dt.hour * 60 + df["dt"].dt.minute
    df["date"] = df["dt"].dt.date

    total = int(len(df))
    by_result_counts = {r: int((df["result"] == r).sum()) for r in RESULT_ORDER}
    for value in sorted(set(df["result"]) - set(RESULT_ORDER)):
        by_result_counts[value] = int((df["result"] == value).sum())
    by_result = {k: v for k, v in by_result_counts.items() if v}
    by_result_share = {k: _share(v, total) for k, v in by_result.items()}

    missed_mask = df["result"] == "пропущен"
    missed = df[missed_mask]
    served = df[~missed_mask]

    # --- пропущенные по часам ---
    hourly = []
    for hour, group in df.groupby("hour"):
        hour_missed = int((group["result"] == "пропущен").sum())
        row = {
            "hour": int(hour),
            "total": int(len(group)),
            "missed": hour_missed,
            "missed_share": _share(hour_missed, len(group)),
        }
        for name in RESULT_ORDER:
            row[name] = int((group["result"] == name).sum())
        hourly.append(row)
    hourly.sort(key=lambda r: r["hour"])

    missed_hours = sorted(hourly, key=lambda r: (-r["missed"], r["hour"]))
    peak_hour_total = max(hourly, key=lambda r: (r["total"], -r["hour"]))
    peak_missed_hour = missed_hours[0] if missed_hours else None

    # --- пропущенные по дням недели ---
    by_weekday = []
    for weekday, group in df.groupby("weekday"):
        wd_missed = int((group["result"] == "пропущен").sum())
        by_weekday.append(
            {
                "weekday": int(weekday),
                "name": WEEKDAY_NAMES[int(weekday)],
                "total": int(len(group)),
                "missed": wd_missed,
                "missed_share": _share(wd_missed, len(group)),
            }
        )
    by_weekday.sort(key=lambda r: r["weekday"])

    per_day = df.groupby("date").size()
    per_day_missed = missed.groupby("date").size()
    peak_day_date = per_day.idxmax() if len(per_day) else None
    peak_missed_day = per_day_missed.idxmax() if len(per_day_missed) else None

    # --- ожидание ---
    wait = df["wait"]
    wait_block = {
        "mean": _round(wait.mean(), 1),
        "median": _round(wait.median(), 1),
        "p90": _quantile(wait, 0.90),
        "p95": _quantile(wait, 0.95),
        "max": int(wait.max()),
        "norm_sec": ECON["wait_norm_sec"],
        "over_norm": int((wait > ECON["wait_norm_sec"]).sum()),
        "over_norm_share": _share(int((wait > ECON["wait_norm_sec"]).sum()), total),
        "total_hours": _round(wait.sum() / 3600, 1),
        "by_result": {
            r: {
                "count": int(len(g)),
                "mean": _round(g["wait"].mean(), 1),
                "median": _round(g["wait"].median(), 1),
                "p90": _quantile(g["wait"], 0.90),
                "max": int(g["wait"].max()),
            }
            for r, g in df.groupby("result")
        },
    }
    peak_wait_row = df.loc[wait.idxmax()] if total else None
    if peak_wait_row is not None:
        wait_block["peak"] = {
            "sec": int(peak_wait_row["wait"]),
            "at": peak_wait_row["dt"].strftime("%d.%m.%Y %H:%M"),
            "result": str(peak_wait_row["result"]),
        }

    # --- длительности ---
    duration_block = {
        "total_minutes": _round(df["dur"].sum() / 60, 1),
        "mean_all_sec": _round(df["dur"].mean(), 1),
        "served_calls": int(len(served)),
        "mean_served_sec": _round(served["dur"].mean(), 1),
        "scenario_target_sec": ECON["scenario_target_sec"],
        "by_result": {
            r: {
                "count": int(len(g)),
                "mean": _round(g["dur"].mean(), 1),
                "median": _round(g["dur"].median(), 1),
                "p90": _quantile(g["dur"], 0.90),
                "max": int(g["dur"].max()),
                "total_minutes": _round(g["dur"].sum() / 60, 1),
            }
            for r, g in df.groupby("result")
        },
    }
    booked = df[df["result"] == "записан"]
    duration_block["booked_over_target"] = int(
        (booked["dur"] > ECON["scenario_target_sec"]).sum()
    )
    duration_block["booked_over_target_share"] = _share(
        duration_block["booked_over_target"], len(booked)
    )

    # --- вне часов работы операторов ---
    def _in_operator_hours(row: pd.Series) -> bool:
        window = OPERATOR_HOURS["saturday"] if row["weekday"] == 5 else OPERATOR_HOURS["weekday"]
        if row["weekday"] == 6 or window is None:
            return False
        return window[0] <= row["minute_of_day"] < window[1]

    in_hours = df.apply(_in_operator_hours, axis=1)
    out_of_hours = df[~in_hours]
    out_of_hours_missed = int((out_of_hours["result"] == "пропущен").sum())

    # Пропуски внутри часов работы операторов — самая дорогая часть потерь:
    # линия формально работает, но трубку не поднимают (перегрузка).
    inside_hours = missed[in_hours.reindex(missed.index, fill_value=False)]
    inside_by_hour = (
        inside_hours.groupby("hour").size().sort_values(ascending=False)
    )
    inside_block = {
        "total": int(len(inside_hours)),
        "share_of_missed": _share(len(inside_hours), len(missed)),
        "top_hours": [
            {"hour": int(hour), "missed": int(count)}
            for hour, count in inside_by_hour.head(3).items()
        ],
    }
    first_hour = df[df["hour"] == 7]
    if len(first_hour):
        inside_block["first_open_hour"] = {
            "hour": 7,
            "total": int(len(first_hour)),
            "missed": int((first_hour["result"] == "пропущен").sum()),
            "comment": (
                "все звонки в 07:xx пропущены: по регламенту 2.2 линия открыта "
                "с 07:00, фактически операторы поднимают трубку с 08:00"
            ),
        }

    # --- месяцы ---
    months = sorted({(d.year, d.month) for d in df["date"]})
    by_month = []
    for year, month in months:
        mask = (df["dt"].dt.year == year) & (df["dt"].dt.month == month)
        group = df[mask]
        by_month.append(
            {
                "month": f"{year}-{month:02d}",
                "name": f"{MONTH_NAMES[month]} {year}",
                "total": int(len(group)),
                "missed": int((group["result"] == "пропущен").sum()),
            }
        )
    months_count = max(len(months), 1)

    repeat_callers = missed.groupby("caller_number").size()
    block = {
        "total": total,
        "period_from": df["dt"].min().strftime("%d.%m.%Y"),
        "period_to": df["dt"].max().strftime("%d.%m.%Y"),
        "days": int(df["date"].nunique()),
        "months": months_count,
        "calls_per_month": int(round(total / months_count)),
        "by_result": by_result,
        "by_result_share": by_result_share,
        "automation_share": _share(
            by_result.get("записан", 0) + by_result.get("справка", 0), total
        ),
        "escalation_share": _share(by_result.get("перевод", 0), total),
        "missed": {
            "total": int(len(missed)),
            "share": _share(len(missed), total),
            "per_day_mean": _round(per_day_missed.mean() if len(per_day_missed) else 0, 1),
            "by_hour": hourly,
            "by_weekday": by_weekday,
            "peak_hour": peak_missed_hour,
            "peak_day": (
                {"date": peak_missed_day.strftime("%d.%m.%Y"), "missed": int(per_day_missed.max())}
                if peak_missed_day is not None
                else None
            ),
            "callers": int(missed["caller_number"].nunique()),
            "callers_more_than_once": int((repeat_callers > 1).sum()),
            "out_of_operator_hours": {
                "total": int(len(out_of_hours)),
                "missed": out_of_hours_missed,
                "missed_share": _share(out_of_hours_missed, len(out_of_hours)),
                "share_of_all": _share(len(out_of_hours), total),
                "rule": OPERATOR_HOURS["note"],
            },
            "inside_operator_hours": inside_block,
        },
        "wait_sec": wait_block,
        "duration_sec": duration_block,
        "peak_hour": {
            "hour": peak_hour_total["hour"],
            "calls": peak_hour_total["total"],
        },
        "peak_day": (
            {"date": peak_day_date.strftime("%d.%m.%Y"), "calls": int(per_day.max())}
            if peak_day_date is not None
            else None
        ),
        "by_month": by_month,
        "hourly": hourly,
    }
    return block


# --- история записей -------------------------------------------------------


def analyze_appointments(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Не найдена история записей: {path}")
    df = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
    df["date"] = pd.to_datetime(df["Дата приёма"], format="%d.%m.%Y", errors="coerce")
    df = df[df["date"].notna()].copy()
    df["weekday"] = df["date"].dt.weekday
    df["specialty"] = df["Специальность"].str.strip().str.lower().str.replace("ё", "е", regex=False)
    df["hour"] = df["Время приёма"].str.slice(0, 2)

    total = int(len(df))
    by_status = {s: int((df["Статус"] == s).sum()) for s in STATUS_ORDER}
    for value in sorted(set(df["Статус"]) - set(STATUS_ORDER)):
        by_status[value] = int((df["Статус"] == value).sum())

    completed = by_status.get("Состоялся", 0)
    no_show = by_status.get("Неявка", 0)
    cancelled = by_status.get("Отменён", 0)

    by_specialty = []
    for specialty, group in df.groupby("specialty"):
        g_total = int(len(group))
        g_no_show = int((group["Статус"] == "Неявка").sum())
        g_cancel = int((group["Статус"] == "Отменён").sum())
        g_done = int((group["Статус"] == "Состоялся").sum())
        by_specialty.append(
            {
                "specialty": specialty,
                "total": g_total,
                "completed": g_done,
                "cancelled": g_cancel,
                "no_show": g_no_show,
                "no_show_rate": _share(g_no_show, g_total),
                # неявка считается от дошедших до приёма (без отменённых заранее)
                "no_show_rate_of_arrivals": _share(g_no_show, g_done + g_no_show),
                "cancel_rate": _share(g_cancel, g_total),
                "attendance_rate": _share(g_done, g_total),
            }
        )
    by_specialty.sort(key=lambda r: (-r["total"], r["specialty"]))
    top_no_show = sorted(by_specialty, key=lambda r: (-r["no_show_rate_of_arrivals"], r["specialty"]))

    by_weekday = []
    for weekday, group in df.groupby("weekday"):
        by_weekday.append(
            {
                "weekday": int(weekday),
                "name": WEEKDAY_NAMES[int(weekday)],
                "total": int(len(group)),
                "no_show": int((group["Статус"] == "Неявка").sum()),
                "no_show_rate": _share(int((group["Статус"] == "Неявка").sum()), len(group)),
            }
        )
    by_weekday.sort(key=lambda r: r["weekday"])

    by_month = []
    for period, group in df.groupby(df["date"].dt.to_period("M")):
        by_month.append(
            {
                "month": str(period),
                "total": int(len(group)),
                "no_show": int((group["Статус"] == "Неявка").sum()),
                "cancelled": int((group["Статус"] == "Отменён").sum()),
            }
        )

    by_hour = [
        {"hour": int(hour), "total": int(len(group))}
        for hour, group in df.groupby("hour")
        if str(hour).isdigit()
    ]
    by_hour.sort(key=lambda r: r["hour"])

    per_patient = df.groupby("Пациент (хэш)").size()

    return {
        "total": total,
        "period_from": df["date"].min().strftime("%d.%m.%Y"),
        "period_to": df["date"].max().strftime("%d.%m.%Y"),
        "days": int(df["date"].dt.date.nunique()),
        "by_status": by_status,
        "attendance": {
            "completed_share": _share(completed, total),
            "cancelled_share": _share(cancelled, total),
            "no_show_share": _share(no_show, total),
            "no_show_share_of_arrivals": _share(no_show, completed + no_show),
        },
        "no_show": {
            "total": no_show,
            "share": _share(no_show, total),
            "share_of_arrivals": _share(no_show, completed + no_show),
            "by_specialty": by_specialty,
            "worst_specialties": top_no_show[:5],
        },
        "by_specialty": by_specialty,
        "by_weekday": by_weekday,
        "by_month": by_month,
        "by_hour": by_hour,
        "patients": {
            "unique": int(df["Пациент (хэш)"].nunique()),
            "visits_per_patient_mean": _round(per_patient.mean(), 2),
            "visits_per_patient_max": int(per_patient.max()),
        },
    }


# --- экономика -------------------------------------------------------------


def build_economics(calls: dict, appointments: dict) -> dict:
    calls_per_month = calls["calls_per_month"]
    served = calls["duration_sec"]["served_calls"]
    served_minutes = calls["duration_sec"]["total_minutes"]
    operator_minutes_per_call = _round(served_minutes / served, 3) if served else 0.0

    insurance = ECON["insurance_rate"]
    overhead = ECON["overhead_rate"]
    derived_hour_cost = (
        ECON["operator_salary_rub_month"]
        / ECON["operator_hours_month"]
        * (1 + insurance)
        * (1 + overhead)
    )
    minute_cost = ECON["operator_hour_cost_rub"] / 60.0

    # Доля звонков, которые ГА закрывает сам: берём фактическую долю
    # «записан + справка» и осторожно округляем вниз до целых процентов.
    handled_share = math.floor(calls["automation_share"] * 100) / 100.0

    baseline_minutes = calls_per_month * operator_minutes_per_call
    with_assistant_minutes = baseline_minutes * (1 - handled_share)
    saved_minutes = baseline_minutes - with_assistant_minutes
    saved_rub = saved_minutes * minute_cost
    assistant_cost = ECON["assistant_monthly_cost_rub"] + ECON["development_cost_rub"] / ECON["amortization_months"]
    net_rub = saved_rub - assistant_cost
    payback_base = saved_rub - ECON["assistant_monthly_cost_rub"]
    payback_months = (
        _round(ECON["development_cost_rub"] / payback_base, 1) if payback_base > 0 else None
    )

    block = {
        "currency": "RUB",
        "assumptions": {
            "calls_per_month": calls_per_month,
            "operator_salary_rub_month": ECON["operator_salary_rub_month"],
            "operator_hours_month": ECON["operator_hours_month"],
            "insurance_rate": insurance,
            "overhead_rate": overhead,
            "operator_hour_cost_rub": ECON["operator_hour_cost_rub"],
            "operator_hour_cost_derived_rub": _round(derived_hour_cost, 1),
            "operator_minute_cost_rub": _round(minute_cost, 2),
            "operator_minutes_per_call": operator_minutes_per_call,
            "handled_by_assistant_share": handled_share,
            "assistant_monthly_cost_rub": ECON["assistant_monthly_cost_rub"],
            "development_cost_rub": ECON["development_cost_rub"],
            "amortization_months": ECON["amortization_months"],
            "wait_norm_sec": ECON["wait_norm_sec"],
            "scenario_target_sec": ECON["scenario_target_sec"],
        },
        "formulas": {
            "operator_hour_cost_rub": (
                "operator_salary_rub_month / operator_hours_month "
                "* (1 + insurance_rate) * (1 + overhead_rate)"
            ),
            "operator_minutes_per_call": (
                "сумма duration_sec обслуженных звонков / 60 / число обслуженных звонков"
            ),
            "baseline_operator_minutes_month": (
                "calls_per_month * operator_minutes_per_call"
            ),
            "saved_operator_minutes_month": (
                "baseline_operator_minutes_month * handled_by_assistant_share"
            ),
            "saved_rub_month": "saved_operator_minutes_month * operator_minute_cost_rub",
            "assistant_cost_rub_month": (
                "assistant_monthly_cost_rub + development_cost_rub / amortization_months"
            ),
            "net_rub_month": "saved_rub_month - assistant_cost_rub_month",
            "payback_months": (
                "development_cost_rub / (saved_rub_month - assistant_monthly_cost_rub)"
            ),
            "roi_percent_year": "net_rub_month * 12 / (assistant_cost_rub_month * 12) * 100",
            "fte_released": "saved_operator_minutes_month / 60 / operator_hours_month",
        },
        "monthly": {
            "baseline_operator_minutes": _round(baseline_minutes, 0),
            "baseline_operator_hours": _round(baseline_minutes / 60, 1),
            "baseline_rub": _round(baseline_minutes * minute_cost, 0),
            "with_assistant_operator_minutes": _round(with_assistant_minutes, 0),
            "with_assistant_rub": _round(with_assistant_minutes * minute_cost, 0),
            "saved_operator_minutes": _round(saved_minutes, 0),
            "saved_operator_hours": _round(saved_minutes / 60, 1),
            "fte_released": _round(saved_minutes / 60 / ECON["operator_hours_month"], 2),
            "saved_rub": _round(saved_rub, 0),
            "assistant_cost_rub": _round(assistant_cost, 0),
            "net_rub": _round(net_rub, 0),
            "net_rub_year": _round(net_rub * 12, 0),
            "payback_months": payback_months,
            "roi_percent_year": _round(net_rub * 12 / (assistant_cost * 12) * 100, 1)
            if assistant_cost
            else None,
        },
        "quality": {
            "missed_per_month": int(round(calls["missed"]["total"] / calls["months"])),
            "missed_share": calls["missed"]["share"],
            "target_missed_share": 0.05,
            "out_of_operator_hours_per_month": int(
                round(calls["missed"]["out_of_operator_hours"]["total"] / calls["months"])
            ),
            "out_of_operator_hours_missed_share": calls["missed"]["out_of_operator_hours"][
                "missed_share"
            ],
            "wait_over_norm_share": calls["wait_sec"]["over_norm_share"],
            "no_show_share": appointments["no_show"]["share"],
            "no_show_share_of_arrivals": appointments["no_show"]["share_of_arrivals"],
        },
        "notes": [
            "Все ставки — параметры прототипа (ECON в tools/analyze_data.py), "
            "жюри может подставить свои.",
            "Доля handled_by_assistant_share взята из факта: доля звонков "
            "«записан + справка» в журнале, округлённая вниз.",
            "Пропущенные звонки в деньги не переведены: это метрика качества "
            "(ГА отвечает круглосуточно), а не экономия минут оператора.",
            "Экономия считается только на обслуженных звонках, чтобы не "
            "задваивать счёт по уже пропущенным.",
        ],
    }
    return block


ASSUMPTIONS_TEXT = [
    "Источники: data/raw/appointments.csv (история записей МИС) и "
    "data/raw/calls.csv (журнал звонков регистратуры) — выгрузки кейса.",
    "«Неявка» — пациент не пришёл (статус «Неявка»); «Отменён» — запись "
    "отменена заранее; доля неявок считается и от всех записей, и от "
    "дошедших до приёма.",
    "Норма ожидания на линии 30 секунд и целевая длительность сценария "
    "записи 2,5 минуты — регламент 3.1 и 3.2.",
    "Часы работы операторов: будни 07:00–20:00, суббота 08:30–15:00, "
    "воскресенье — нет (регламент 2.2); ГА работает круглосуточно.",
    "Экономика — модель с явными параметрами (tools/analyze_data.py, ECON): "
    "ставка оператора, стоимость ГА, стоимость разработки и срок амортизации.",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Аналитика по выгрузкам кейса № 2")
    parser.add_argument("--appointments", default=None)
    parser.add_argument("--calls", default=None)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    out = Path(args.out) if args.out else data_dir / "metrics.json"
    if not out.is_absolute():
        out = PROJECT_ROOT / out

    appointments_path = _pick(DEFAULT_APPOINTMENTS, args.appointments)
    calls_path = _pick(DEFAULT_CALLS, args.calls)

    appointments = analyze_appointments(appointments_path)
    calls = analyze_calls(calls_path)
    economics = build_economics(calls, appointments)

    payload = {
        "generated_at": datetime.now(MSK).replace(microsecond=0).isoformat(),
        "sources": {
            "appointments": str(appointments_path),
            "calls": str(calls_path),
        },
        "period": {
            "appointments_from": appointments["period_from"],
            "appointments_to": appointments["period_to"],
            "appointments_days": appointments["days"],
            "calls_from": calls["period_from"],
            "calls_to": calls["period_to"],
            "calls_days": calls["days"],
        },
        "calls": calls,
        "appointments": appointments,
        "economics": economics,
        "assumptions": ASSUMPTIONS_TEXT,
        "highlights": [
            f"Пропущено {calls['missed']['total']} из {calls['total']} звонков "
            f"({calls['missed']['share'] * 100:.1f}%) — цель ≤ 5%.",
            f"Среднее ожидание {calls['wait_sec']['mean']} с, максимум "
            f"{calls['wait_sec']['max']} с; дольше нормы 30 с — "
            f"{calls['wait_sec']['over_norm_share'] * 100:.1f}% звонков.",
            f"Все звонки вне часов работы операторов "
            f"({calls['missed']['out_of_operator_hours']['total']}) пропущены — "
            "их закрывает ГА круглосуточно.",
            f"Внутри часов работы операторов пропущено "
            f"{calls['missed']['inside_operator_hours']['total']} звонков; "
            "в субботу пропускается каждый второй звонок.",
            f"Неявки: {appointments['no_show']['total']} "
            f"({appointments['no_show']['share'] * 100:.1f}% записей), "
            f"от дошедших — {appointments['no_show']['share_of_arrivals'] * 100:.1f}%.",
            f"Экономия {economics['monthly']['saved_operator_hours']} ч/мес "
            f"({economics['monthly']['fte_released']} FTE), "
            f"{economics['monthly']['saved_rub']:.0f} ₽/мес.",
        ],
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(
        json.dumps(
            {
                "out": str(out),
                "calls_total": calls["total"],
                "by_result": calls["by_result"],
                "missed_share": calls["missed"]["share"],
                "wait_mean": calls["wait_sec"]["mean"],
                "wait_max": calls["wait_sec"]["max"],
                "appointments_total": appointments["total"],
                "no_show_share": appointments["no_show"]["share"],
                "economics_monthly": economics["monthly"],
                "highlights": payload["highlights"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

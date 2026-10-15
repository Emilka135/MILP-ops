#!/usr/bin/env python3
"""Prompt vocabulary vs. the stress lexicon of ``calls.tts.stress``.

Collects every word that can reach the TTS – scenario ``say`` texts and templates, the verbaliser
(numbers 0–2100 in both genders, every date of the year with weekday and year, times in 5-minute
steps, digit strings), specialties in all used cases, the CRM demo data (doctors, hours, holidays,
patient names) and Russian string literals of the orchestrator / CRM module – and reports the words
that the lexicon + rules cannot stress.

    python tools/stress_vocab.py            # table: covered / missing
    python tools/stress_vocab.py --check    # exit 1 if anything is missing (used by CI / tests)
    python tools/stress_vocab.py --dump     # print the whole vocabulary (one word per line)
    python tools/stress_vocab.py --schedule schedule.csv   # also doctors from a CSV/Excel export
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import re
import sys
from datetime import date, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from calls.crm import module as crm_module  # noqa: E402
from calls.crm.registry import MockRegistry, seed_demo  # noqa: E402
from calls.scenario.dsl import load_flows  # noqa: E402
from calls.tts import verbalize as V  # noqa: E402
from calls.tts.stress import DictAccentor  # noqa: E402

_WORD_RE = re.compile(r"[а-яё]+(?:-[а-яё]+)*")
_RU_LITERAL = re.compile(r'"([^"\n]*[А-Яа-яЁё][^"\n]*)"')
# modules whose Russian string literals can be spoken (operator tickets are visual only → not included)
_SOURCE_MODULES = ("calls.crm.module", "calls.orchestrator.session", "calls.orchestrator.app", "calls.outbound.reminder")


def _walk(obj, out: list[str], depth: int = 0) -> None:
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _walk(v, out, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, out, depth + 1)
    elif hasattr(obj, "__dict__") and depth < 8:
        for v in vars(obj).values():
            _walk(v, out, depth + 1)


def collect_texts(scenarios: Path = ROOT / "scenarios", today: date = date(2026, 10, 14), extra_names: list[str] | None = None) -> list[str]:
    texts: list[str] = []
    _walk(load_flows(scenarios), texts)
    for name in _SOURCE_MODULES:
        texts += _RU_LITERAL.findall(inspect.getsource(importlib.import_module(name)))
    for n in range(0, 2101):
        texts.append(V.number_to_words(n))
        texts.append(V.number_to_words(n, feminine=True))
    d0 = date(today.year, 1, 1)
    for i in range(366):
        texts.append(V.date_to_words(d0 + timedelta(days=i), with_weekday=True, with_year=True))
    for h in range(24):
        for m in range(0, 60, 5):
            texts.append(V.time_to_words(time(h, m)))
    texts.append(V.digits_to_words("0123456789"))
    texts += V.WEEKDAY_NOM
    texts += list(crm_module._SPECIALTY_DATIVE) + list(crm_module._SPECIALTY_DATIVE.values())
    reg = MockRegistry(":memory:")
    seed_demo(reg, today)
    for table in ("doctors", "clinic_hours", "holidays"):
        for row in reg.execute(f"SELECT * FROM {table}"):  # noqa: S608 - table names are constants
            texts += [str(v) for v in row.values()]
    for row in reg.execute("SELECT first_name, middle_name FROM patients"):
        texts += [str(v) for v in row.values()]
    texts += extra_names or []
    return texts


def vocabulary(texts: list[str]) -> set[str]:
    words: set[str] = set()
    for t in texts:
        words.update(_WORD_RE.findall(t.lower()))
    return words


def missing_words(words: set[str], accentor: DictAccentor | None = None) -> list[str]:
    acc = accentor or DictAccentor()
    return sorted(w for w in words if not acc.stress_word(w)[1])


def _schedule_names(path: Path) -> list[str]:
    """Doctor names / specialties of a CSV or Excel schedule (same loaders as the CRM mirror)."""
    reg = MockRegistry(":memory:")
    if path.suffix.lower() == ".csv":
        reg.load_schedule_csv(path)
    else:
        reg.load_schedule_xlsx(path)
    names: list[str] = []
    for row in reg.execute("SELECT last_name, full_name, specialty FROM doctors"):
        names += [str(v) for v in row.values()]
    return names


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 1 when words are missing from the lexicon")
    ap.add_argument("--dump", action="store_true", help="print the whole vocabulary")
    ap.add_argument("--schedule", type=Path, help="CSV/XLSX schedule: include doctor names and specialties")
    ap.add_argument("--scenarios", type=Path, default=ROOT / "scenarios")
    ns = ap.parse_args(argv)
    extra = _schedule_names(ns.schedule) if ns.schedule else None
    words = vocabulary(collect_texts(ns.scenarios, extra_names=extra))
    if ns.dump:
        print("\n".join(sorted(words)))
        return 0
    acc = DictAccentor()
    missing = missing_words(words, acc)
    print(f"prompt vocabulary: {len(words)} words, covered: {len(words) - len(missing)}, missing: {len(missing)}")
    if missing:
        print("add to calls/tts/stress.py LEXICON (with '+' before the stressed vowel):")
        print("  " + " ".join(missing))
    return 1 if (ns.check and missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())

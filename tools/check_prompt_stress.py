"""Preflight current root scenarios AFTER normalization, demo prompts and abbreviations.

Unlike the legacy runtime tool, this checks the actual root scenario directory and
expanded medical abbreviations. It never treats unresolved words as a success.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "runtime/src")]
from calls.scenario.dsl import load_flows
from calls.tts.normalize import ABBREVIATIONS, LETTERS, normalize_text
from calls.tts.stress import DictAccentor, require_stress_marks
from clinic.demo_call import speech_phrases
from clinic.agent import offer_text


def collect_prompts(scenarios: Path | None = None) -> list[dict]:
    records = []
    for flow in load_flows(scenarios or ROOT / "scenarios").values():
        for state in flow.states.values():
            for statement in state.statements:
                if statement.kind == "say":
                    # Static parts of templates also contribute vocabulary. The
                    # actual patient-dependent values must pass the runtime gate.
                    text = re.sub(r"\{[^{}]+\}", " ", statement.text)
                    records.append(
                        {
                            "source": f"{flow.name}.{state.name}",
                            "text": text,
                            "kind": "template_static"
                            if text != statement.text
                            else "static",
                        }
                    )
    records += [
        {"source": "demo_call", "text": text, "kind": "demo"}
        for text in speech_phrases()
    ]
    records += [
        {
            "source": f"normalizer.abbreviation.{abbr}",
            "text": abbr,
            "kind": "abbreviation",
        }
        for abbr in ABBREVIATIONS
    ]
    records += [
        {"source": f"normalizer.letter.{letter}", "text": spoken, "kind": "letter"}
        for letter, spoken in LETTERS.items()
    ]
    records += [
        {
            "source": "dynamic.date",
            "text": f"Вы записаны 21.10.{year} в 14:30, кабинет 201.",
            "kind": "dynamic_example",
        }
        for year in (2000, 2026, 2030, 2040, 2050, 2060, 2070, 2080, 2090, 2099)
    ]
    records += [
        {"source": "diagnostic.real_patient_input", "text": text, "kind": "acceptance_input"}
        for text in (
            "Хочу записаться к кардиологу.",
            "Пожалуйста, говорите немного медленнее.",
            "Да, конечно.",
            "Позовите оператора.",
            "Завтра в пятнадцать часов.",
            "У меня острая боль в груди.",
        )
    ]
    records += [
        {"source": "agent.canonical_offer", "text": offer_text(datetime(2026, 10, 16, 8, 15) + timedelta(days=day)), "kind": "dynamic_example"}
        for day in range(7)
    ]
    return records


def check_prompts(
    records: list[dict], accentor: DictAccentor | None = None
) -> list[dict]:
    accentor = accentor or DictAccentor()
    results = []
    for record in records:
        item = dict(record)
        try:
            normalized = normalize_text(record["text"])
            missing = sorted(set(accentor.coverage(normalized)))
            stressed = accentor(normalized)
            item.update(
                normalized_text=normalized,
                stressed_text=stressed,
                missing_words=missing,
            )
            require_stress_marks(stressed)
            item["status"] = "passed" if not missing else "missing_stress"
        except ValueError as error:
            item.update(
                status="missing_stress"
                if item.get("missing_words")
                else "invalid_text",
                error=str(error),
            )
        results.append(item)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=ROOT / "scenarios")
    parser.add_argument(
        "--text",
        action="append",
        default=[],
        help="Also preflight a new prompt or explicitly stressed name",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "debug/runs/prompt-stress")
    parser.add_argument("--strict", action="store_true", help="Fail on dictionary gaps (diagnostic mode only)")
    args = parser.parse_args(argv)
    records = collect_prompts(args.scenarios)
    records += [
        {"source": "extra_text", "text": text, "kind": "extra"} for text in args.text
    ]
    results = check_prompts(records)
    missing = sorted(
        {word for item in results for word in item.get("missing_words", [])}
    )
    failed = [item for item in results if item["status"] == "invalid_text" or (args.strict and item["status"] != "passed")]
    report = {
        "status": "failed" if failed else "passed",
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "root_scenarios": str(args.scenarios.resolve()),
        "checked_phrases": len(results),
        "failed_phrases": len(failed),
        "missing_words": missing,
        "manual_stress_required": False,
        "results": results,
        "limitation": "Coverage is not linguistic approval of a stress position or a patient surname.",
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "results"},
            ensure_ascii=False,
        )
    )
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())

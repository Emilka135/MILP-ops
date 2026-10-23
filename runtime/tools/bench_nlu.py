"""Speed/accuracy benchmark of the two NLU approaches on the same utterances.

    A. classifier → switch-case   (``calls.nlu.intent.classify`` → flow ``intent`` edges; optionally the
                                   two-agent verifier ``TwoAgentNLU`` with the hash embedder or USER-bge-m3)
    B. deterministic text → SQL   (``calls.nlu.text2sql.compile_query`` → one parametrised SQL statement)

Both are run on the same corpus (clean + ASR-noise corrupted), timed per utterance, and the result of
B is executed against the SQLite mirror so that the measurement includes the real query cost.

    python tools/bench_nlu.py                 # 2 000 utterances, hash embedder (no downloads)
    python tools/bench_nlu.py --embedder bge  # requires sentence-transformers + deepvk/USER-bge-m3 (CPU)
    python tools/bench_nlu.py --json reports/bench_nlu.json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from calls.crm.registry import MockRegistry, seed_demo  # noqa: E402
from calls.nlu.agent import TwoAgentNLU  # noqa: E402
from calls.nlu.embed import EmbeddingIntentClassifier, HashEmbedder  # noqa: E402
from calls.nlu.intent import classify  # noqa: E402
from calls.nlu.text2sql import compile_query  # noqa: E402
from tests.asr_noise import corrupt  # noqa: E402

TODAY = date(2026, 10, 14)

# (template, gold intent, gold text2sql kind)
TEMPLATES = [
    ("запишите меня к {spec} {day}", "book", "find_slots"),
    ("хочу записаться к {spec} {day} {part}", "book", "find_slots"),
    ("мне нужен {spec}, лучше {day}", "book", "find_slots"),
    ("можно к {spec} в {time}", "book", "find_slots"),
    ("запишите ребёнка к {spec}", "book", "find_slots"),
    ("хочу перенести запись {day}", "reschedule", "reschedule_candidates"),
    ("перенесите пожалуйста {day} {part}", "reschedule", "reschedule_candidates"),
    ("не смогу прийти, отмените запись", "cancel", "cancel_booking"),
    ("отмените мою запись пожалуйста", "cancel", "cancel_booking"),
    ("подтверждаю, приду", "confirm", "confirm_booking"),
    ("когда у меня запись", "my_booking", "my_booking"),
    ("во сколько работает поликлиника в {wd}", "info_schedule", "clinic_hours"),
    ("вы работаете в праздники", "info_holidays", "holidays"),
    ("какой у вас адрес", "info_address", "clinic_info"),
    ("нужно ли направление к {spec}", "referral", "referral"),
    ("хочу пожаловаться на врача", "complaint", None),
    ("у меня острая боль в груди", "emergency", None),
    ("можно ли пить эти таблетки с алкоголем", "medical", None),
    ("соедините с оператором", "operator", None),
    ("к доктору {surname} {day}", "book", None),  # specific surname → escalate
]
SPECS = ["терапевту", "кардиологу", "лору", "хирургу", "неврологу", "окулисту", "педиатру", "эндокринологу"]
DAYS = ["на завтра", "на послезавтра", "в пятницу", "в понедельник", "на четырнадцатое октября", "двадцатого", "на следующей неделе"]
PARTS = ["утром", "после обеда", "вечером", "в первой половине дня", ""]
TIMES = ["десять утра", "четырнадцать тридцать", "9:15", "пол третьего", "16.00"]
WDS = ["субботу", "воскресенье", "понедельник"]
SURNAMES = ["Волкову", "Петрову", "Смирновой"]


def corpus(n: int, seed: int) -> list[tuple[str, str, str | None]]:
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        tpl, intent, kind = rnd.choice(TEMPLATES)
        text = tpl.format(spec=rnd.choice(SPECS), day=rnd.choice(DAYS), part=rnd.choice(PARTS), time=rnd.choice(TIMES),
                          wd=rnd.choice(WDS), surname=rnd.choice(SURNAMES)).replace("  ", " ").strip()
        out.append((text, intent, kind))
    return out


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def summarize(name: str, lat_us: list[float], correct: int, total: int, extra: dict | None = None) -> dict:
    row = {
        "approach": name,
        "n": total,
        "accuracy": round(correct / total, 4) if total else None,
        "p50_us": round(pct(lat_us, 50), 1),
        "p95_us": round(pct(lat_us, 95), 1),
        "p99_us": round(pct(lat_us, 99), 1),
        "mean_us": round(statistics.fmean(lat_us), 1) if lat_us else None,
        "throughput_per_s": round(1e6 / statistics.fmean(lat_us), 0) if lat_us else None,
    }
    row.update(extra or {})
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--severity", type=float, default=0.5, help="ASR-noise severity for the noisy pass (0..1)")
    ap.add_argument("--embedder", choices=["hash", "bge"], default="hash")
    ap.add_argument("--json", type=Path, default=None)
    ns = ap.parse_args(argv)

    reg = MockRegistry()
    seed_demo(reg, TODAY)
    clean = corpus(ns.n, ns.seed)
    noisy = [(corrupt(t, seed=i, severity=ns.severity), g, k) for i, (t, g, k) in enumerate(clean)]

    if ns.embedder == "bge":
        from calls.nlu.embed import SentenceTransformerEmbedder

        embedder = SentenceTransformerEmbedder("deepvk/USER-bge-m3")
    else:
        embedder = HashEmbedder()
    two = TwoAgentNLU(embed=EmbeddingIntentClassifier(embedder).fit())

    rows = []
    for label, data in (("clean", clean), (f"noisy@{ns.severity}", noisy)):
        # A1: rules only
        lat, ok = [], 0
        for text, gold, _ in data:
            t0 = time.perf_counter_ns()
            r = classify(text, TODAY)
            lat.append((time.perf_counter_ns() - t0) / 1000)
            ok += r.intent == gold
        rows.append(summarize(f"A1 rules→switch-case [{label}]", lat, ok, len(data)))

        # A2: rules + verifier agent
        lat, ok, esc = [], 0, 0
        for text, gold, _ in data:
            t0 = time.perf_counter_ns()
            d = two.decide(text, TODAY, have=["patient", "booking"])
            lat.append((time.perf_counter_ns() - t0) / 1000)
            ok += d.intent == gold
            esc += d.action == "operator"
        rows.append(summarize(f"A2 two-agent ({ns.embedder}) [{label}]", lat, ok, len(data), {"to_operator": esc}))

        # B: text → SQL (+ execution on the mirror)
        lat, lat_exec, ok, esc = [], [], 0, 0
        for text, gold, kind in data:
            t0 = time.perf_counter_ns()
            q = compile_query(text, TODAY)
            t1 = time.perf_counter_ns()
            if q.sql and not q.needs:
                reg.execute(q.sql, q.params)
            t2 = time.perf_counter_ns()
            lat.append((t1 - t0) / 1000)
            lat_exec.append((t2 - t0) / 1000)
            if kind is None:
                ok += q.escalate
            else:
                ok += q.kind == kind
            esc += q.escalate
        rows.append(summarize(f"B text→SQL compile [{label}]", lat, ok, len(data), {"to_operator": esc}))
        rows.append(summarize(f"B text→SQL compile+execute [{label}]", lat_exec, ok, len(data)))

    hdr = f"{'approach':46} {'n':>5} {'acc':>6} {'p50 µs':>8} {'p95 µs':>8} {'p99 µs':>8} {'req/s':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['approach']:46} {r['n']:>5} {r['accuracy']:>6.3f} {r['p50_us']:>8.0f} {r['p95_us']:>8.0f} {r['p99_us']:>8.0f} {r['throughput_per_s']:>8.0f}")
    if ns.json:
        ns.json.parent.mkdir(parents=True, exist_ok=True)
        ns.json.write_text(json.dumps({"today": TODAY.isoformat(), "rows": rows}, ensure_ascii=False, indent=2))
        print(f"\nwritten {ns.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

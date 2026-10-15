#!/usr/bin/env python3
"""Run the ITU-T Q.24 acceptance matrix for the in-band DTMF detector and write a report.

    python tools/dtmf_matrix.py                 # full factorial (72 000 cases, multi-core)
    python tools/dtmf_matrix.py --quick          # OFAT + 400 random cases
    python tools/dtmf_matrix.py --out reports/dtmf_matrix.md

Exit status is 0 only if every "detect" case produced exactly the right digit and every
"reject" case produced nothing. "any" cases (between the Q.24 operate and non-operate
limits) are reported for information.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from calls.dtmf.matrix import AXES, Case, full_factorial, one_factor_at_a_time, random_subset, run_case  # noqa: E402
from calls.dtmf.params import DtmfParams  # noqa: E402


def _run(case: Case) -> tuple[Case, str, bool, int, int]:
    o = run_case(case)
    start_lat = o.starts[0].t_detect - o.t_on if o.starts else -1
    end_lat = o.ends[0].t_detect - o.t_off if o.ends else -1
    return case, o.digits, o.passed, start_lat, end_lat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="reports/dtmf_matrix.md")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = ap.parse_args()

    cases = one_factor_at_a_time() + random_subset(400) if args.quick else list(full_factorial())
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        results = list(ex.map(_run, cases, chunksize=64))
    elapsed = time.time() - t0

    by_expected = Counter()
    failed_by_expected = Counter()
    any_detected = 0
    clipped_detected = 0
    failures = []
    per_axis = defaultdict(lambda: Counter())
    lat_start = []
    lat_end = []
    for case, digits, passed, sl, el in results:
        exp = case.effective_expected
        by_expected[exp] += 1
        if not passed:
            failed_by_expected[exp] += 1
            failures.append((case, digits))
        if exp == "any" and digits:
            any_detected += 1
        if exp == "clipped" and digits == case.digit:
            clipped_detected += 1
        if exp == "detect" and passed:
            lat_start.append(sl)
            lat_end.append(el)
        for axis in AXES:
            key = (axis, getattr(case, axis))
            per_axis[key]["total"] += 1
            per_axis[key]["fail"] += 0 if passed else 1

    p = DtmfParams()
    lines = [
        "# DTMF in-band detector — ITU-T Q.24 acceptance matrix",
        "",
        f"- detector version: `{p.version}` (N={p.window}, hop={p.hop}, min_duration={p.min_duration_ms} ms, "
        f"level ≥ {p.level_min_dbm0} dBm0, twist {p.twist_min_db}…{p.twist_max_db} dB ±{p.measurement_margin_db}, "
        f"freq tol ±{p.freq_tol * 100:g} %)",
        f"- cases: {len(cases)} ({'quick subset' if args.quick else 'full factorial'}), wall time {elapsed:.1f} s on {args.jobs} workers",
        "",
        "| expected | cases | failed |",
        "|---|---:|---:|",
    ]
    for exp in ("detect", "reject", "any", "clipped"):
        lines.append(f"| {exp} | {by_expected[exp]} | {failed_by_expected[exp]} |")
    lines += [
        "",
        f"- `any` cases that produced a digit: {any_detected} / {by_expected['any']}",
        f"- `clipped` = operate cells whose stimulus (tone + 4σ noise) exceeds the 16-bit range; "
        f"detected in {clipped_detected} / {by_expected['clipped']}",
    ]
    if lat_start:
        ls = sorted(lat_start)
        le = sorted(lat_end)
        q = lambda a, f: a[min(len(a) - 1, int(f * len(a)))] / 8.0  # noqa: E731
        lines += [
            f"- DigitStart latency after onset (detect cases): p50 {q(ls, 0.5):.1f} ms, p95 {q(ls, 0.95):.1f} ms, "
            f"p99 {q(ls, 0.99):.1f} ms, max {ls[-1] / 8:.1f} ms (budget 60 ms p95)",
            f"- DigitEnd latency after tone end: p50 {q(le, 0.5):.1f} ms, p95 {q(le, 0.95):.1f} ms, "
            f"p99 {q(le, 0.99):.1f} ms, max {le[-1] / 8:.1f} ms (budget 40 ms p95)",
        ]
    lines += ["", "## Failures per axis value", "", "| axis | value | failed / total |", "|---|---|---:|"]
    for (axis, value), c in sorted(per_axis.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        if c["fail"]:
            lines.append(f"| {axis} | {value} | {c['fail']} / {c['total']} |")
    if failures:
        lines += ["", "## First failures", "", "| case | expected | got |", "|---|---|---|"]
        for case, digits in failures[:200]:
            lines.append(f"| {case.label()} | {case.expected} | `{digits}` |")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:12]))
    print(f"\nreport: {out}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

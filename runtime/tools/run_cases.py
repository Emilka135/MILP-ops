#!/usr/bin/env python3
"""Run the open test cases (input: caller actions → output: records in the stand) and print the input/output table.

    python tools/run_cases.py                       # in-process orchestrator + fake stand (CI)
    python tools/run_cases.py --only reminder       # subset
    python tools/run_cases.py --md reports/cases.md --json reports/cases.json
    python tools/run_cases.py --orchestrator http://localhost:8080 --stand http://localhost:8087 --crm http://localhost:8081
                                                    # against the compose stack (crm owns the outbox there)

Exit code 1 when any case fails – the organisers' autoruns judge exactly this: the right record in the stand.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from calls.cases.runner import RemoteEnv, format_table, load_cases, run_all  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="case files (default: cases/*.json)")
    ap.add_argument("--only", default=None, help="substring of the case id")
    ap.add_argument("--md", default=None, help="write the input/output table as Markdown")
    ap.add_argument("--json", default=None, help="write the per-case results as JSON")
    ap.add_argument("--orchestrator", default=None, help="run against a deployed orchestrator (URL)")
    ap.add_argument("--stand", default=None, help="URL of the fake stand (needed with --orchestrator)")
    ap.add_argument("--crm", default=None, help="URL of the crm service that owns the outbox (compose); default = orchestrator")
    ap.add_argument("--token", default="team-token")
    ap.add_argument("--timeout", type=float, default=10.0, help="seconds to wait for each expected state")
    ns = ap.parse_args(argv)

    cases = load_cases([Path(f) for f in ns.files] or None)
    env_factory = None
    if ns.orchestrator:
        if not ns.stand:
            ap.error("--stand is required with --orchestrator")
        env_factory = lambda c: RemoteEnv(c, ns.orchestrator, ns.stand, ns.crm, ns.token)  # noqa: E731
    results = run_all(cases, only=ns.only, env_factory=env_factory, timeout=ns.timeout)
    table = format_table(results)
    print(table)
    passed = sum(r.ok for r in results)
    print(f"\n{passed}/{len(results)} cases passed")
    if os.environ.get("GITHUB_ACTIONS") == "true":  # failing cases as annotations (logs/artifacts are not always reachable)
        for r in results:
            if not r.ok:
                got = ", ".join(f"{w['method']} {w['path']} → {w['status']}" for w in r.writes) or "no writes"
                print(f"::error title=case {r.case_id} ({r.status})::{'; '.join(r.problems)} | stand got: {got}")
    if ns.md:
        Path(ns.md).parent.mkdir(parents=True, exist_ok=True)
        Path(ns.md).write_text("# Открытые тест-кейсы: вход → выход\n\n" + table + f"\n\n**{passed}/{len(results)} passed**\n", encoding="utf-8")
    if ns.json:
        Path(ns.json).parent.mkdir(parents=True, exist_ok=True)
        Path(ns.json).write_text(json.dumps([r.row() for r in results], ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

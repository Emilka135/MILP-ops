"""Reproducible test entrypoint. All generated output belongs to debug/runs/."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "debug/runs/checks"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ["integration", "legacy", "nlu"]:
        (OUT.parent / name).mkdir(parents=True, exist_ok=True)
    steps = [
        ("rust-format", ["cargo", "fmt", "--all", "--check"], ROOT, {}),
        (
            "rust-clippy",
            ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
            ROOT,
            {},
        ),
        ("rust-tests", ["cargo", "test", "--locked"], ROOT, {}),
        (
            "rust-build",
            ["cargo", "build", "--locked", "--bin", "clinic-voice"],
            ROOT,
            {},
        ),
        (
            "integration",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/integration",
                "--junitxml=debug/runs/integration/results.xml",
                "--basetemp=debug/runs/integration/pytest-temp",
            ],
            ROOT,
            {},
        ),
        (
            "calls-regression",
            [
                sys.executable,
                "-m",
                "pytest",
                "--junitxml=../debug/runs/legacy/calls.xml",
                "--basetemp=../debug/runs/legacy/pytest-temp",
            ],
            ROOT / "runtime",
            {"PYTHONPATH": "src"},
        ),
        (
            "milp-regression",
            [sys.executable, "debug/run_legacy_milp.py"],
            ROOT,
            {"PYTHONPATH": "."},
        ),
        (
            "nlu-benchmark",
            [sys.executable, "debug/run_nlu_benchmark.py"],
            ROOT,
            {},
        ),
    ]
    results = []
    for name, command, cwd, extra in steps:
        with (OUT / (name + ".log")).open("w") as log:
            run = subprocess.run(
                command,
                cwd=cwd,
                env={**os.environ, **extra},
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        results.append(
            {
                "check": name,
                "passed": run.returncode == 0,
                "log": str((OUT / (name + ".log")).relative_to(ROOT)),
            }
        )
        print(name + (": PASS" if run.returncode == 0 else ": FAIL"), flush=True)
    (OUT / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    raise SystemExit(any(not r["passed"] for r in results))


if __name__ == "__main__":
    main()

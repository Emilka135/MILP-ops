"""Repeat both Rust classifiers; preserve variation instead of claiming a stable speedup."""

import json
import subprocess
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]


def main():
    subprocess.run(
        ["cargo", "build", "--release", "--locked", "--bin", "bench-nlu"],
        cwd=ROOT,
        check=True,
    )
    rows = []
    for _ in range(5):
        result = subprocess.run(
            [str(ROOT / "target/release/bench-nlu")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        rows.append(json.loads(result.stdout))
    report = {
        "runs": rows,
        "median_rules_ns": median(x["rules_ns_per_request"] for x in rows),
        "median_automaton_ns": median(x["automaton_ns_per_request"] for x in rows),
        "median_speedup": median(x["automaton_speedup"] for x in rows),
        "scope": "classification only, same vocabulary, no ASR/network",
    }
    folder = ROOT / "debug/runs/nlu"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

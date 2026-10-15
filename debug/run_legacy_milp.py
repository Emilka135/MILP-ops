"""MILP tests are executable scripts, not pytest modules (some exit at import time)."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
files = [
    "tests/test_ru.py",
    "tests/test_mis.py",
    "tests/test_safety.py",
    "tests/test_slots.py",
    "tests/smoke_engine.py",
    "tests/acceptance.py",
    "tools/check_frontend_contract.py",
]
failed = []
for file in files:
    run = subprocess.run(
        [sys.executable, file],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    print(f"{file}: {'PASS' if run.returncode == 0 else 'FAIL'}")
    if run.returncode:
        failed.append(file)
        print(run.stdout[-5000:] + run.stderr[-3000:])
sys.exit(bool(failed))

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_real_speech_requests_callback_but_emergency_transfers():
    folder = ROOT / "debug/runs/callback"
    folder.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "OPERATOR_AVAILABLE": "0",
        "CRM_NOW": "2026-10-15T23:00:00+04:00",
        "DEMO_DATA_DIR": str(folder / "test-stack"),
    }
    with (folder / "test-stack.log").open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "tools/dev.py", "--no-build", "--base-port", "28280"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            base = "http://127.0.0.1:28280"
            for _ in range(250):
                if proc.poll() is not None:
                    raise AssertionError((folder / "test-stack.log").read_text())
                try:
                    with urllib.request.urlopen(base + "/readyz", timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("offline operator stack did not become ready")
            sys.path.insert(0, str(ROOT / "debug"))
            from run_callback import execute

            report = execute(base, "http://127.0.0.1:28285", folder)
            assert set(report["results"].values()) == {"passed"}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

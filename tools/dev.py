"""Run six black boxes and the real Rust server locally; Ctrl-C stops every child."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--base-port", type=int, default=8080)
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--python", default=os.environ.get("CLINIC_PYTHON", sys.executable))
    p.add_argument(
        "--stand-url",
        default="",
        help="optional local contract test stand (not the real organisers' stand)",
    )
    ns = p.parse_args()
    if not ns.no_build:
        subprocess.run(
            ["cargo", "build", "--locked", "--bin", "clinic-voice"],
            cwd=ROOT,
            check=True,
        )
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT) + os.pathsep + str(ROOT / "runtime/src"),
        "APP_MODE": "demo",
        "PYTHONUNBUFFERED": "1",
        "STAND_URL": ns.stand_url,
        "SMS_GATEWAY_URL": "",
        "API_TOKEN": "",
        "TTS_MODEL": str(ROOT / "modules/tts/models/v5_cis_base.pt"),
        "ASR_MODEL_PATH": str(ROOT / "modules/asr/models"),
        "ASR_QUANTIZATION": os.environ.get("ASR_QUANTIZATION", "int8"),
    }
    store = Path(env.get("DEMO_DATA_DIR", str(ROOT / "debug/runs/local-stack")))
    store.mkdir(parents=True, exist_ok=True)
    env["CRM_DB"] = str(store / "clinic.sqlite")
    env["BIND"] = f"127.0.0.1:{ns.base_port}"
    modules = ["crm", "nlu", "sms", "operator", "tts", "asr"]
    for index, module in enumerate(modules, 1):
        env[module.upper() + "_URL"] = f"http://127.0.0.1:{ns.base_port + index}"
    procs, files = [], []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        commands = [
            [ns.python, "-m", "clinic", m, "--port", str(ns.base_port + i)]
            for i, m in enumerate(modules, 1)
        ]
        commands.append([str(ROOT / "target/debug/clinic-voice")])
        for label, cmd in zip(modules + ["orchestrator"], commands):
            log = (store / f"{label}.log").open("w")
            files.append(log)
            procs.append(
                subprocess.Popen(
                    cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
                )
            )
        for port in range(ns.base_port, ns.base_port + 7):
            deadline = time.monotonic() + 30
            while True:
                if any(p.poll() is not None for p in procs):
                    raise RuntimeError(f"service failed; inspect {store}/*.log")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=1
                    ):  # nosec B310 -- fixed local HTTP scheme
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"service {port} not ready")
                    time.sleep(0.1)
        base = f"http://127.0.0.1:{ns.base_port}"
        print(
            f"Ready: {base}; operator: {base}/operator. Synthetic CRM; real Silero and ASR.",
            flush=True,
        )
        if ns.smoke:
            sys.path.insert(0, str(ROOT / "debug"))
            from run_http_smoke import smoke
            from run_speech_commands import execute

            print(json.dumps(smoke(base), ensure_ascii=False, indent=2))

            report = execute(base, env["TTS_URL"], mode="combined")
            (store / "speech-commands.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
            print(json.dumps(report["results"], ensure_ascii=False), flush=True)
        else:
            while not stopping:
                if any(p.poll() is not None for p in procs):
                    raise RuntimeError(f"service failed; inspect {store}/*.log")
                time.sleep(0.2)
    finally:
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in files:
            log.close()


if __name__ == "__main__":
    main()

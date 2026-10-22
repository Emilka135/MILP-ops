"""Twenty concurrent real audio calls; deadlines include ingress, inference and reply PCM.

Use only the standalone compose.acceptance.yaml stack. WAVs are frozen inputs:
this runner never calls speech synthesis to regenerate fixtures or injects ASR text.
"""
from __future__ import annotations

import argparse
from array import array
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import wave


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def request(base, op, args=None, *, timeout=59):
    body = None if args is None else json.dumps(args).encode()
    url = base.rstrip("/") + ("/" + op if args is None else "/v1/" + op)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=max(.1, timeout)) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise AssertionError("oversize response")
    if args is None and op == "metrics":
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode()
    result = json.loads(raw)
    if args is not None:
        if result.get("status") != "ok":
            raise AssertionError(result)
        return result["payload"]
    return result


def signal(pcm, rate):
    samples = array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    rms = math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))
    peak = max((abs(x) for x in samples), default=0)
    if rate not in {8000, 16000, 24000, 48000} or len(samples) < rate // 10 or rms <= 20 or peak <= 100:
        raise AssertionError(f"empty/silent/invalid PCM: {rate=}, {rms=}, {peak=}")
    return {"duration_s": len(samples) / rate, "rms": round(rms, 2), "peak": peak}


def fixtures(folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    items = []
    for item in manifest["items"]:
        path = folder / item["file"]
        if path.parent.resolve() != folder.resolve():
            raise AssertionError("fixture path escapes corpus")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise AssertionError("frozen corpus checksum changed: " + item["id"])
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth()) != (1, 2):
                raise AssertionError("fixture must be mono PCM16")
            rate, pcm = wav.getframerate(), wav.readframes(wav.getnframes())
        signal(pcm, rate)
        items.append({**item, "rate": rate, "pcm_b64": base64.b64encode(pcm).decode()})
    return items


def drain(base, call_id, folder, label, deadline):
    clips = []
    for i in range(12):
        remaining = min(10, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("response audio/playback deadline")
        clip = request(base, "call.next_prompt", {"call_id": call_id}, timeout=remaining)
        if "pcm_b64" not in clip:
            break
        if not str(clip.get("engine", "")).startswith("silero"):
            raise AssertionError("reply must use the real Silero backend")
        pcm = base64.b64decode(clip["pcm_b64"], validate=True)
        rate = int(clip["sample_rate"])
        measurements = signal(pcm, rate)
        output = folder / call_id / f"{label}-{i + 1}.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(pcm)
        clips.append({"file": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                      "engine": clip["engine"], "prompt_id": clip.get("prompt_id"), **measurements})
    else:
        raise AssertionError("response clip drain exceeded 12 clips")
    if not clips:
        raise AssertionError("turn produced no playable response audio")
    request(base, "call.prompt_done", {"call_id": call_id, "prompt_id": clips[-1]["prompt_id"]},
            timeout=max(.1, min(10, deadline - time.monotonic())))
    return clips


def execute(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    items = fixtures(Path(args.corpus))
    with tempfile.TemporaryDirectory(prefix="clinic-load-graph-") as folder:
        subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "tools/compile_rust.py"), folder],
                       check=True, timeout=10, capture_output=True)
        expected_revision = re.search(r'SCENARIO_REVISION: &str = "([a-f0-9]+)"',
                                      (Path(folder) / "scenarios.rs").read_text())[1]
    meta = request(args.crm, "meta", {})
    if meta.get("stand_mode") != "demo" or meta.get("clinic_name") != "Acceptance isolated clinic":
        raise AssertionError("runner requires the isolated disposable acceptance CRM")
    operator = next(i for i in items if i["id"] == "operator-1")
    varied = [i for i in items if i["id"] != "operator-1"]
    if len({i["voice"] for i in varied}) < 2 or min(i["speed"] for i in varied) > .75 or max(i["speed"] for i in varied) < 2.5:
        raise AssertionError("corpus lacks fixed diverse voices / 0.75–2.5 speeds")
    before = request(args.crm, "snapshot", {})
    metrics_before = request(args.base, "metrics", {})
    began = time.monotonic()
    run_id = "load-" + uuid.uuid4().hex[:12]
    barriers = [threading.Barrier(20) for _ in range(3)]
    results = []
    metrics_samples = []
    sample_stop = threading.Event()

    def sample():
        while not sample_stop.wait(.5):
            try:
                metrics_samples.append({"elapsed_s": time.monotonic() - began, "metrics": request(args.base, "metrics", {}, timeout=2)})
            except Exception as error:
                metrics_samples.append({"error": str(error)})
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()

    def one(index):
        call_id = f"{run_id}-{index:02}"
        registrar = str(8100 + index)
        record = {"call_id": call_id, "registrar": registrar, "turns": [], "status": "running"}
        try:
            barriers[0].wait(timeout=65)
            start = time.monotonic()
            flow = "inbound" if args.mode == "dtmf" else "voice"
            state = request(args.base, "call.start", {"call_id": call_id, "flow": flow, "registrar_number": registrar}, timeout=59)
            record["start_audio"] = drain(args.base, call_id, output, "welcome", start + 59)
            record["start_s"] = time.monotonic() - start
            if record["start_s"] >= 60:
                raise AssertionError("start exceeded 60s")
            for turn in range(2):
                barriers[turn + 1].wait(timeout=65)
                item = varied[index % len(varied)] if turn == 0 else operator
                audio = args.mode == "speech" or args.mode == "mixed" and turn == 0
                payload = {"call_id": call_id}
                if audio:
                    payload.update(pcm_b64=item["pcm_b64"], rate=item["rate"])
                else:
                    payload["digit"] = "1" if turn == 0 else "*"
                start = time.monotonic()
                state = request(args.base, "call.audio" if audio else "call.dtmf", payload, timeout=59)
                clips = drain(args.base, call_id, output, f"turn-{turn + 1}", start + 59)
                elapsed = time.monotonic() - start
                entry = {"turn": turn + 1, "input": "real_asr_audio" if audio else "dtmf", "elapsed_s": elapsed,
                         "fixture_id": item["id"] if audio else None, "fixture_sha256": item["sha256"] if audio else None,
                         "voice": item["voice"] if audio else None, "speed": item["speed"] if audio else None,
                         "state": state["state"], "reply_audio": clips}
                record["turns"].append(entry)
                save(output / call_id / "result.json", record)
                if elapsed >= 60:
                    raise AssertionError(f"turn exceeded 60s: {elapsed}")
                if state.get("errors") or state.get("sms"):
                    raise AssertionError({"errors": state.get("errors"), "unexpected_sms": state.get("sms")})
            target = "callback.ask_consent"
            archived = request(args.crm, "learning.call", {"call_id": call_id, "registrar": registrar}, timeout=10)
            save(output / call_id / "archive.json", {**archived, "expected_target": target})
            if state["state"] != target or state.get("finished"):
                raise AssertionError({"wrong_target": state["state"], "expected": target})
            turns = [t for t in archived["transcript"] if t["role"] in {"patient", "dtmf"}]
            expected_audio = 2 if args.mode == "speech" else 1 if args.mode == "mixed" else 0
            if archived["call_id"] != call_id or archived["registrar"] != registrar or len(turns) != 2:
                raise AssertionError("call/registrar isolation or transcript length failed")
            if sum(t.get("source") == "real_asr" for t in turns) != expected_audio:
                raise AssertionError("audio was not archived as actual ASR")
            if archived["trace"][-1] != target or archived.get("scenario_revision") != expected_revision:
                raise AssertionError("missing target trace/revision")
            if any(t.get("state") not in archived["trace"] for t in turns):
                raise AssertionError("transcript state does not belong to this call trace")
            if args.mode == "speech" and "оператор" not in turns[-1]["text"].lower():
                raise AssertionError("operator WAV was not actually recognized")
            record.update(status="passed", target=target, trace=archived["trace"], transcript=turns,
                          scenario_revision=archived["scenario_revision"])
        except Exception as error:
            record.update(status="failed", error=f"{type(error).__name__}: {error}")
            for barrier in barriers:
                barrier.abort()
        save(output / call_id / "result.json", record)
        return record

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(one, index) for index in range(20)]
            for future in as_completed(futures, timeout=args.run_timeout - 10):
                results.append(future.result())
    finally:
        sample_stop.set()
        sampler.join(timeout=3)
    after = request(args.crm, "snapshot", {}, timeout=10)
    operations = request(args.crm, "stand.status", {}, timeout=10)
    pending_sms = request(args.crm, "sms_pending", {}, timeout=10)
    asr_probe = request(args.asr, "transcribe", {"pcm_b64": operator["pcm_b64"], "rate": operator["rate"]})
    actual_asr = (str(asr_probe.get("engine", "")).startswith("onnx-asr:")
                  and asr_probe.get("provider") in {"CPUExecutionProvider", "CUDAExecutionProvider"}
                  and (asr_probe["provider"] != "CUDAExecutionProvider" or asr_probe.get("cuda_verified") is True))
    modules = {"crm": args.crm, "nlu": args.nlu, "tts": args.tts, "asr": args.asr}
    module_metrics = {name: request(base, "metrics", timeout=5) for name, base in modules.items()}
    elapsed = [t["elapsed_s"] for r in results for t in r["turns"]]
    ordered = sorted(elapsed)
    good = (len(results) == 20 and all(r["status"] == "passed" for r in results)
            and len(elapsed) == 40 and max(elapsed, default=60) < 60
            and before == after and not operations["operations"] and not pending_sms["messages"] and actual_asr)
    report = {"status": "passed" if good else "failed", "synthetic": True,
              "mode": args.mode, "concurrent_calls": 20, "barrier_parties": 20,
              "total_s": time.monotonic() - began, "turns": len(elapsed),
              "max_turn_s": max(elapsed, default=None), "p95_turn_s": ordered[math.ceil(.95 * len(ordered)) - 1] if ordered else None,
              "target": "callback.ask_consent", "no_booking_changes": before == after,
              "expected_scenario_revision": expected_revision, "actual_asr_probe": asr_probe,
              "no_operations": not operations["operations"], "no_sms": not pending_sms["messages"],
              "metrics_before": metrics_before, "metrics_after": request(args.base, "metrics", {}),
              "metrics_samples": metrics_samples, "module_metrics": module_metrics, "calls": results,
              "scope": "real audio/ASR/NLU/Rust/TTS; safe operator escalation, no appointment throughput claim",
              "latency_origin": "after complete frozen user PCM is available; caller speaking duration excluded",
              "vad_scope": "finished utterance ingress; noise endpointing requires separate tests"}
    save(output / "report.json", report)
    print(json.dumps({k: report[k] for k in ("status", "mode", "concurrent_calls", "turns", "max_turn_s", "p95_turn_s", "total_s")}, ensure_ascii=False))
    return 0 if good else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:33880")
    parser.add_argument("--crm", default="http://127.0.0.1:33881")
    parser.add_argument("--nlu", default="http://127.0.0.1:33882")
    parser.add_argument("--tts", default="http://127.0.0.1:33885")
    parser.add_argument("--asr", default="http://127.0.0.1:33886")
    parser.add_argument("--corpus", default="tests/fixtures/speech_regression")
    parser.add_argument("--output", default="debug/runs/concurrent-calls")
    parser.add_argument("--mode", choices=("speech", "dtmf", "mixed"), default="speech")
    parser.add_argument("--run-timeout", type=float, default=240)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 90 <= args.run_timeout <= 600:
        parser.error("run timeout must be 90–600 seconds")
    if args.child:
        try:
            return execute(args)
        except Exception as error:
            save(Path(args.output) / "preflight-failure.json", {"status": "failed", "error": f"{type(error).__name__}: {error}"})
            raise
    # Hard process deadline also covers stuck HTTP/playback/worker teardown.
    child = subprocess.Popen([sys.executable, __file__, *sys.argv[1:], "--child"])
    try:
        return child.wait(timeout=args.run_timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)
        save(Path(args.output) / "deadline.json", {"status": "failed", "error": "whole-run deadline exceeded", "timeout_s": args.run_timeout})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

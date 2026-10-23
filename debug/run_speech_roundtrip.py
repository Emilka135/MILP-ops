"""Real audio benchmark: Silero -> G.711 -> ONNX ASR -> normaliser -> optional Rust call.

There is no simulation or text fallback. Model/file errors fail the command.
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "runtime/src")]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--orchestrator", default="")
    p.add_argument("--rates", nargs="+", type=float, default=[1.3, 1.5, 1.75])
    p.add_argument(
        "--output", default="debug/runs/speech-only/recognition-quality.json"
    )
    ns = p.parse_args()
    os.environ.setdefault("ASR_MODEL_PATH", str(ROOT / "modules/asr/models"))
    import numpy as np
    from calls.asr.onnx_gateway import OnnxAsrModule
    from calls.audio.g711 import alaw_decode, alaw_encode
    from calls.tts.silero import SileroTTS, to_pcm16
    from clinic.modules import Nlu

    tts = SileroTTS(
        ROOT / "modules/tts/models/v5_cis_base.pt", "ru_ekaterina", stress="dict"
    )
    asr = OnnxAsrModule()
    nlu = Nlu()
    cases = [
        ("intent", "отмените запись", "cancel"),
        ("intent", "перенесите запись", "reschedule"),
        ("intent", "подтверждаю приём", "confirm"),
        ("intent", "запишите к терапевту", "book"),
        ("intent", "у меня острая боль в груди", "emergency"),
        ("yes_no", "да, подходит", True),
        ("yes_no", "нет, не подходит", False),
        ("said_date", "четырнадцатого октября", "2026-10-14"),
        ("said_time", "в четырнадцать тридцать", "14:30"),
        ("said_daypart", "после обеда", "afternoon"),
    ]
    rows = []
    for rate in ns.rates:
        for kind, phrase, expected in cases:
            start = time.perf_counter()
            audio = tts.synth(phrase, rate)
            synth_s = time.perf_counter() - start
            pcm = np.frombuffer(to_pcm16(audio), dtype=np.int16)
            decoded = alaw_decode(alaw_encode(pcm))
            status, result = asr.handle(
                "transcribe",
                {"pcm_b64": base64.b64encode(decoded.tobytes()).decode(), "rate": 8000},
            )
            if status != "ok":
                raise RuntimeError(result)
            _, parsed = nlu.handle(
                "interpret", {"text": result["text"], "today": "2026-10-15"}
            )
            actual = (
                parsed.get(kind)
                if kind in ("intent", "yes_no")
                else parsed["vars"].get(kind)
            )
            row = {
                "source": phrase,
                "rate": rate,
                "recognized": result["text"],
                "kind": kind,
                "expected": expected,
                "actual": actual,
                "correct": actual == expected,
                "audio_s": len(audio) / 8000,
                "tts_s": synth_s,
                "asr_rtf": result["rtf"],
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    if ns.orchestrator:
        from run_speech_commands import execute

        execute(ns.orchestrator, ns.orchestrator, mode="speech-only")
    report = {
        "mode": "real-audio",
        "tts": "silero-v5-cis-base",
        "asr": asr.model_name,
        "channel": "G.711 A-law, 8kHz",
        "rows": rows,
        "correct": sum(r["correct"] for r in rows),
        "total": len(rows),
    }
    output = ROOT / ns.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

"""Closed loop  text → TTS → (G.711 + noise) → ASR → normalisation → compare.

The point is to measure the thing the booking flow actually depends on: do *numbers, dates, times and
yes/no answers* survive the telephone channel and the recogniser, and does ``calls.nlu.normalize``
bring the many surface forms ("четырнадцатое", "14-е", "14.10", "четырнадцатого октября") back to one value.

Back-ends (all optional – the tool falls back to simulation so it runs in CI without models):

  TTS  --tts silero    modules/tts/models/v5_cis_base.pt via torch (MIT; speaker ru_zhadyra, lexicon stress, 8 kHz, rate 1.3–1.75)
       --tts stub      silence of the right duration (pipeline/timing check only)
  ASR  --asr onnx      onnx-asr  gigaam-v3-e2e-rnnt / gigaam-v3-e2e-ctc / t-one  (pip install onnx-asr)
       --asr deeray    Deeray GigaAM v3 runtime, POST /v1/transcribe?model=rnnt (--url)
       --asr vosk      vosk-model-small-ru-0.22 / vosk-model-ru-0.42 (--vosk-model DIR)
       --asr sim       text-level ASR-noise simulation (tests/asr_noise.corrupt) – the CI default

    python tools/e2e_tts_asr.py                                   # simulation, 3 severities, prints the table
    python tools/e2e_tts_asr.py --tts silero --asr onnx --asr-model gigaam-v3-e2e-rnnt --rates 1.0 1.3 1.5 1.75
    python tools/e2e_tts_asr.py --asr deeray --url http://asr:8080 --bench   # also reports audio-s/s of the runtime
    python tools/e2e_tts_asr.py --json reports/e2e_tts_asr.json --md docs/E2E_TTS_ASR.md
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time as _time
import wave
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from calls.audio.g711 import alaw_decode, alaw_encode  # noqa: E402
from calls.nlu.normalize import interpret  # noqa: E402
from calls.tts.silero import SAMPLE_RATE, StubTTS, load_synth  # noqa: E402
from calls.tts.verbalize import date_to_words, digits_to_words, number_to_words, time_to_words  # noqa: E402
from tests.asr_noise import corrupt  # noqa: E402

TODAY = date(2026, 10, 14)


# ---- test set --------------------------------------------------------------------------------------------


@dataclass
class Case:
    category: str  # number | date | time | daypart | digits | yes_no
    spoken: str  # what the "patient" says (TTS input)
    expected: object  # value after normalisation


def build_cases() -> list[Case]:
    cases: list[Case] = []
    for n in (1, 2, 3, 7, 11, 12, 14, 15, 17, 19, 20, 21, 25, 30, 31, 40, 101, 1980, 1992):
        cases.append(Case("number", number_to_words(n), n))
    for d in (date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 21), date(2026, 11, 2), date(2026, 12, 30)):
        cases.append(Case("date", "на " + date_to_words(d, with_weekday=False), d))
        cases.append(Case("date", "запишите на " + date_to_words(d, with_weekday=False), d))
    for phrase, d in (("завтра", date(2026, 10, 15)), ("послезавтра", date(2026, 10, 16)), ("в пятницу", date(2026, 10, 16)),
                      ("в понедельник", date(2026, 10, 19)), ("в следующую среду", date(2026, 10, 21)), ("через неделю", date(2026, 10, 21))):
        cases.append(Case("date", phrase, d))
    for t in (time(9, 0), time(9, 15), time(10, 30), time(12, 0), time(14, 30), time(15, 45), time(16, 0), time(17, 20)):
        cases.append(Case("time", "в " + time_to_words(t), t))
    for phrase, t in (("в половине третьего", time(14, 30)), ("пол десятого утра", time(9, 30)), ("без пятнадцати два", time(13, 45)),
                      ("в три часа дня", time(15, 0)), ("в десять утра", time(10, 0)), ("в шесть вечера", time(18, 0))):
        cases.append(Case("time", phrase, t))
    for phrase, p in (("утром", "morning"), ("после обеда", "afternoon"), ("во второй половине дня", "afternoon"), ("вечером", "evening"),
                      ("в первой половине дня", "morning"), ("ближе к обеду", "midday")):
        cases.append(Case("daypart", phrase, p))
    for digits in ("1002", "12051980", "12345678901", "79035551234"):
        cases.append(Case("digits", digits_to_words(digits), digits))
    for phrase, v in (("да", True), ("да, подтверждаю", True), ("конечно", True), ("ага", True), ("нет", False), ("нет, не приду", False),
                      ("не получится", False), ("отменяю", False)):
        cases.append(Case("yes_no", phrase, v))
    return cases


def check(case: Case, text: str) -> bool:
    it = interpret(text, TODAY)
    if case.category == "number":
        return bool(it.numbers) and it.numbers[0] == case.expected
    if case.category == "date":
        return it.date == case.expected
    if case.category == "time":
        return it.time == case.expected
    if case.category == "daypart":
        return it.daypart == case.expected
    if case.category == "digits":
        return it.digits == case.expected
    if case.category == "yes_no":
        return it.yes_no == case.expected
    return False


# ---- channel ---------------------------------------------------------------------------------------------


def telephone_channel(x: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    """8 kHz float → A-law → float with band-limited noise at the given SNR."""
    pcm = np.clip(x * 32767, -32768, 32767).astype(np.int16)
    y = alaw_decode(alaw_encode(pcm)).astype(np.float32) / 32768
    if snr_db < 90:
        p_sig = float(np.mean(y ** 2)) or 1e-6
        noise = rng.standard_normal(len(y)).astype(np.float32)
        noise *= np.sqrt(p_sig / (10 ** (snr_db / 10)))
        y = y + noise
    return np.clip(y, -1, 1)


def to_wav_bytes(x: np.ndarray, sr: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(x * 32767, -32768, 32767).astype(np.int16).tobytes())
    return buf.getvalue()


# ---- ASR back-ends -------------------------------------------------------------------------------------------


def make_asr(kind: str, model: str, url: str, vosk_model: str) -> Callable[[np.ndarray], str]:
    if kind == "onnx":
        import onnx_asr  # type: ignore

        m = onnx_asr.load_model(model)

        def run(x: np.ndarray) -> str:
            x16 = np.interp(np.arange(0, len(x), SAMPLE_RATE / 16000), np.arange(len(x)), x).astype(np.float32)
            return str(m.recognize(x16, sample_rate=16000))

        return run
    if kind == "deeray":
        import urllib.request

        def run(x: np.ndarray) -> str:
            req = urllib.request.Request(f"{url.rstrip('/')}/v1/transcribe?model=rnnt", data=to_wav_bytes(x), headers={"Content-Type": "audio/wav"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())["text"]

        return run
    if kind == "vosk":
        from vosk import KaldiRecognizer, Model  # type: ignore

        vm = Model(vosk_model)

        def run(x: np.ndarray) -> str:
            rec = KaldiRecognizer(vm, SAMPLE_RATE)
            rec.AcceptWaveform(np.clip(x * 32767, -32768, 32767).astype(np.int16).tobytes())
            return json.loads(rec.FinalResult()).get("text", "")

        return run
    raise SystemExit(f"unknown asr {kind}")


# ---- main ------------------------------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tts", choices=["stub", "silero"], default="stub")
    ap.add_argument("--tts-model", default="modules/tts/models/v5_cis_base.pt")
    ap.add_argument("--rates", type=float, nargs="*", default=[1.0, 1.3, 1.5, 1.75])
    ap.add_argument("--asr", choices=["sim", "onnx", "deeray", "vosk"], default="sim")
    ap.add_argument("--asr-model", default="gigaam-v3-e2e-rnnt")
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--vosk-model", default="models/vosk-model-small-ru-0.22")
    ap.add_argument("--snr", type=float, nargs="*", default=[99, 20, 10], help="SNR dB for the telephone channel (99 = clean)")
    ap.add_argument("--severities", type=float, nargs="*", default=[0.0, 0.5, 1.0], help="sim back-end noise levels")
    ap.add_argument("--bench", action="store_true", help="also report seconds of audio per second")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--md", type=Path, default=None)
    ns = ap.parse_args(argv)

    cases = build_cases()
    rng = np.random.default_rng(0)
    rows: list[dict] = []

    if ns.asr == "sim":
        for sev in ns.severities:
            per_cat: dict[str, list[bool]] = {}
            for i, c in enumerate(cases):
                heard = corrupt(c.spoken, seed=i, severity=sev)
                per_cat.setdefault(c.category, []).append(check(c, heard))
            rows.append({"tts": "n/a", "rate": None, "asr": f"sim@{sev}", "snr": None,
                         **{k: round(sum(v) / len(v), 3) for k, v in per_cat.items()},
                         "total": round(sum(sum(v) for v in per_cat.values()) / len(cases), 3)})
    else:
        synth = load_synth(ns.tts_model) if ns.tts == "silero" else StubTTS()
        if ns.tts == "silero" and isinstance(synth, StubTTS):
            print("WARNING: Silero model not loadable, falling back to stub TTS (ASR will hear silence)", file=sys.stderr)
        asr = make_asr(ns.asr, ns.asr_model, ns.url, ns.vosk_model)
        for rate in ns.rates:
            for snr in ns.snr:
                per_cat = {}
                audio_s = 0.0
                t_asr = 0.0
                for c in cases:
                    x = synth.synth(c.spoken, rate)
                    y = telephone_channel(x, snr, rng)
                    audio_s += len(y) / SAMPLE_RATE
                    t0 = _time.perf_counter()
                    heard = asr(y)
                    t_asr += _time.perf_counter() - t0
                    per_cat.setdefault(c.category, []).append(check(c, heard))
                row = {"tts": synth.name, "rate": rate, "asr": f"{ns.asr}:{ns.asr_model}", "snr": snr,
                       **{k: round(sum(v) / len(v), 3) for k, v in per_cat.items()},
                       "total": round(sum(sum(v) for v in per_cat.values()) / len(cases), 3)}
                if ns.bench:
                    row["audio_s_per_s"] = round(audio_s / t_asr, 1) if t_asr else None
                rows.append(row)

    cats = ["number", "date", "time", "daypart", "digits", "yes_no"]
    hdr = f"{'tts':12} {'rate':>5} {'asr':28} {'snr':>4} " + " ".join(f"{c:>8}" for c in cats) + f" {'total':>7}" + ("  audio-s/s" if ns.bench else "")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['tts']:12} {str(r['rate'] or '-'):>5} {r['asr']:28} {str(r['snr'] or '-'):>4} " + " ".join(f"{r.get(c, 0):>8.3f}" for c in cats)
              + f" {r['total']:>7.3f}" + (f"  {r.get('audio_s_per_s')}" if ns.bench else ""))
    print(f"\n{len(cases)} cases: " + ", ".join(f"{c} {sum(1 for x in cases if x.category == c)}" for c in cats))
    if ns.json:
        ns.json.parent.mkdir(parents=True, exist_ok=True)
        ns.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if ns.md:
        ns.md.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# TTS → ASR → нормализация: доля верно восстановленных значений", "",
                 "| TTS | rate | ASR | SNR dB | " + " | ".join(cats) + " | total |", "|---|---:|---|---:|" + "---:|" * (len(cats) + 1)]
        for r in rows:
            lines.append(f"| {r['tts']} | {r['rate'] or '-'} | {r['asr']} | {r['snr'] or '-'} | " + " | ".join(f"{r.get(c, 0):.3f}" for c in cats) + f" | **{r['total']:.3f}** |")
        ns.md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

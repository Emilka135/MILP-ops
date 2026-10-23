"""CPU-only listening research; never changes the production TTS pipeline or bank.

Run with the existing ML Python: PYTHONPATH=runtime/src python debug/run_voice_listening.py --asr
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import wave
from datetime import UTC, datetime
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime/src"))
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ASR_PROVIDER"] = "CPUExecutionProvider"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ.setdefault("ASR_MODEL_PATH", str(ROOT / "modules/asr/models"))
import numpy as np
from calls.tts.normalize import normalize_text
from calls.tts.processing import process_audio
from calls.tts.silero import SileroTTS, build_ssml, to_pcm16
from calls.tts.stress import LEXICON, DictAccentor

OUT = ROOT / "debug/runs/voice-listening"
RATES = (1.0, 1.15, 1.5)
CASES = (
    ("01-sms", "Мы отправим СМС с подробностями записи."),
    ("02-appointment", "Вы записаны 16.10.2026 в 14:30, кабинет 201."),
    ("03-buttons", "Подтвердите: нажмите 1. Отказ: 0. Регистратор: 2."),
    ("04-first-visit", "Вы у нас впервые?"),
    ("05-procedures", "Возьмите полис ОМС. У вас запись на УЗИ и ЭКГ."),
    ("06-surname", "Врач Хачатурян принимает завтра."),
)
# Listening hypotheses only, never written to the production dictionary.
EXPERIMENTAL = {
    "подробностями": "подр+обностями",
    "впервые": "вперв+ые",
    "подтвердите": "подтверд+ите",
    "регистратор": "регистр+атор",
    "обязательное": "обяз+ательное",
    "медицинское": "медиц+инское",
    "страхование": "страхов+ание",
    "ультразвуковое": "ультразвуков+ое",
    "исследование": "иссл+едование",
    "электрокардиограмма": "электрокардиогр+амма",
}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def wav(path, audio, sample_rate):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(to_pcm16(audio))


def read_wav(path):
    with wave.open(str(path), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise ValueError("Expected mono PCM16 WAV")
        return np.frombuffer(stream.readframes(stream.getnframes()), "<i2").astype(
            np.float64
        ) / 32768, stream.getframerate()


def db(value):
    return round(20 * np.log10(max(float(value), 1e-9)), 2)


def metrics(audio, sr):
    audio = np.asarray(audio, dtype=np.float64)
    frame_size = int(sr * 0.02)
    frames = audio[: len(audio) // frame_size * frame_size].reshape(-1, frame_size)
    levels = np.sqrt(np.mean(frames * frames, axis=1))
    edge_samples = max(1, int(sr * 0.12))
    edges = np.concatenate([audio[:edge_samples], audio[-edge_samples:]])
    fft_size = min(2048, frame_size)
    spectrum = (
        np.abs(np.fft.rfft(frames[:, :fft_size] * np.hanning(fft_size), axis=1)) ** 2
    )
    frequencies = np.fft.rfftfreq(fft_size, 1 / sr)
    energy = spectrum.sum() + 1e-20
    hi = spectrum[:, frequencies >= 4000].sum() / energy if sr > 8000 else 0
    low = spectrum[:, frequencies <= 100].sum() / energy
    return {
        "duration_s": round(len(audio) / sr, 4),
        "peak_dbfs": db(np.max(np.abs(audio))),
        "rms_dbfs": db(np.sqrt(np.mean(audio**2))),
        "clipped_fraction": round(float(np.mean(np.abs(audio) >= 32760 / 32768)), 8),
        "dc_offset": round(float(np.mean(audio)), 7),
        "low_energy_frame_p10_dbfs": db(np.percentile(levels, 10)),
        "high_energy_frame_p95_dbfs": db(np.percentile(levels, 95)),
        "frames_below_minus50dbfs_fraction": round(
            float(np.mean(levels < 10 ** (-50 / 20))), 5
        ),
        "outer_120ms_rms_dbfs": db(np.sqrt(np.mean(edges**2))),
        "energy_above_4khz_fraction": round(float(hi), 6),
        "energy_below_100hz_fraction": round(float(low), 6),
        "noise_only_segment_confirmed": False,
        "noise_metric_limit": "Low-energy frames and utterance edges may contain breaths or consonants; these are not measured background-noise power or SNR.",
    }


def generate_raw(synth, text, accentor):
    ssml, _ = build_ssml(text, 1.0, accentor)
    torch = synth._torch
    with synth._lock, torch.random.fork_rng(devices=[]):
        seed = int.from_bytes(
            hashlib.sha256((synth.speaker + ssml).encode()).digest()[:8], "little"
        )
        torch.manual_seed(seed)
        source = synth.model.apply_tts(
            ssml_text=ssml, speaker=synth.speaker, sample_rate=48000
        )
    return source.detach().cpu().numpy().astype(np.float32), ssml, seed


def save_report(report):
    (OUT / "metadata.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )


def listening_page(report):
    sections = []
    for case in report["cases"]:
        rows = []
        for item in case["variants"]:
            rows.append(
                "<tr><td>"
                + str(item["rate"])
                + "</td>"
                + "".join(
                    f'<td><audio controls preload="none" src="{escape(item[key])}"></audio></td>'
                    for key in ("hq_wav", "telephone_wav")
                )
                + "</tr>"
            )
        sections.append(
            "<section><h2>"
            + escape(case["id"])
            + "</h2><p>"
            + escape(case["text"])
            + "</p><p>OOV: "
            + escape(", ".join(case["missing_stress"]) or "нет")
            + "</p><table><tr><th>Темп</th><th>48 кГц</th><th>8 кГц</th></tr>"
            + "".join(rows)
            + "</table></section>"
        )
    page = (
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Сравнение голоса</title><style>body{font:17px system-ui;max-width:1050px;margin:36px auto;padding:0 20px;background:#fafafa;color:#202020}section{background:white;padding:20px;margin:20px 0;border:1px solid #ddd}td,th{padding:10px}audio{width:320px}</style><h1>Сравнение темпа, полосы и ударений</h1><p>Настоящий Silero v5_cis_base, ru_ekaterina, CPU. В каждой строке одна исходная генерация, разная обработка. Громкость не выровнена усилением. Никакого шумоподавления. HQ и телефонная версия ограничены разной полосой.</p><p>Последние три блока — экспериментальные ударения только для прослушивания; рабочий словарь не менялся. Оценка естественности человеком ещё не выполнена.</p>'
        + "".join(sections)
        + "</html>"
    )
    (OUT / "listen.html").write_text(page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asr", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    synth = SileroTTS(
        ROOT / "modules/tts/models/v5_cis_base.pt",
        speaker="ru_ekaterina",
        device="cpu",
        threads=2,
        stress="dict",
    )
    report = {
        "status": "running",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "model": str(synth.model_path.relative_to(ROOT)),
        "model_sha256": sha(synth.model_path),
        "speaker": synth.speaker,
        "device": "cpu",
        "torch_version": synth._torch.__version__,
        "pipeline_version": synth.pipeline_version,
        "pipeline_files_sha256": {
            n: sha(ROOT / "runtime/src/calls/tts" / n)
            for n in ("silero.py", "stress.py", "processing.py", "normalize.py")
        },
        "ffmpeg_version": subprocess.check_output(
            ["ffmpeg", "-version"], text=True
        ).splitlines()[0],
        "human_listening_status": "pending; acoustic metrics and ASR do not prove naturalness or correct word stress",
        "denoise_applied": False,
        "denoise_reason": "No independently labelled noise-only reference. Noise suppression would risk removing consonants and breath without proving a defect; do not infer noise from model RNG or high-frequency speech energy.",
        "real_asr": bool(args.asr),
        "cases": [],
        "existing_wavs": [],
    }
    for name in ("01-sms", "02-date-time", "03-buttons", "06-procedures"):
        for suffix in ("48khz", "8khz"):
            path = ROOT / "debug/runs/tts-quality/verified" / f"{name}-{suffix}.wav"
            if path.exists():
                audio, sr = read_wav(path)
                report["existing_wavs"].append(
                    {
                        "path": str(path.relative_to(ROOT)),
                        "sha256": sha(path),
                        "sample_rate": sr,
                        **metrics(audio, sr),
                    }
                )
    asr = None
    if args.asr:
        from calls.asr.onnx_gateway import OnnxAsrModule
        from calls.tts.quality import compare

        asr = OnnxAsrModule()
    cases = [(name, text, "production-dict", DictAccentor()) for name, text in CASES]
    manual = DictAccentor(lexicon={**LEXICON, **EXPERIMENTAL})
    cases += [
        (name + "-explicit-stress", text, "experimental-dictionary-only", manual)
        for name, text in CASES
        if name in {"01-sms", "03-buttons", "05-procedures"}
    ]
    for name, text, mode, accentor in cases:
        started = time.monotonic()
        source, ssml, seed = generate_raw(synth, text, accentor)
        source_path = OUT / (name + "-source-48khz.wav")
        wav(source_path, source, 48000)
        case = {
            "id": name,
            "text": text,
            "normalized_text": normalize_text(text),
            "stress_mode": mode,
            "ssml": ssml,
            "seed": seed,
            "missing_stress": sorted(set(accentor.coverage(normalize_text(text)))),
            "source_wav": source_path.name,
            "source_sha256": sha(source_path),
            "source_before_pcm16_metrics": metrics(source, 48000),
            "variants": [],
        }
        for rate in RATES:
            tag = str(rate).replace(".", "p")
            hq, telephone = (
                process_audio(source, rate, 48000),
                process_audio(source, rate, 8000),
            )
            hq_path, phone_path = (
                OUT / f"{name}-rate{tag}-48khz.wav",
                OUT / f"{name}-rate{tag}-8khz.wav",
            )
            wav(hq_path, hq, 48000)
            wav(phone_path, telephone, 8000)
            item = {
                "rate": rate,
                "hq_wav": hq_path.name,
                "telephone_wav": phone_path.name,
                "hq_sha256": sha(hq_path),
                "telephone_sha256": sha(phone_path),
                "hq_metrics": metrics(hq, 48000),
                "telephone_metrics": metrics(telephone, 8000),
            }
            if asr:
                status, result = asr.handle(
                    "transcribe",
                    {
                        "pcm_b64": base64.b64encode(to_pcm16(telephone)).decode(),
                        "rate": 8000,
                    },
                )
                if status != "ok":
                    raise RuntimeError(result)
                item["actual_asr"] = result["text"]
                item["asr_assessment"] = compare(normalize_text(text), result["text"])
            case["variants"].append(item)
            print(name, rate, item.get("actual_asr", ""), flush=True)
        case["elapsed_s"] = round(time.monotonic() - started, 3)
        report["cases"].append(case)
        save_report(report)
    report["status"] = "generated_pending_human_listening"
    report["wav_count"] = len(list(OUT.glob("*.wav")))
    report["production_pipeline_modified"] = False
    save_report(report)
    listening_page(report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "cases": len(report["cases"]),
                "wav_count": report["wav_count"],
                "denoise": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

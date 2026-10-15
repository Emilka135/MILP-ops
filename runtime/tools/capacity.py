"""Capacity model: how many calls per 10 minutes fit on one GPU box (12 / 16 / 24 / 32 GB).

The model is deliberately simple and every number is a named parameter so that it can be re-run
with measured values from the stand (``--asr-audio-s-per-s``, ``--speech-share`` ...).

Resources per call:
  * DTMF / dialogue engine         CPU only, ~0.5 % of a core per call (Goertzel + switch-case) → never the bottleneck
  * TTS (Silero v5, 8 kHz)         CPU or GPU; prompts are cached (LRU) so only dynamic phrases are synthesised
  * ASR streaming (GigaAM v3 CTC)  GPU weights shared, per-stream activations + chunk buffers; compute bound by
                                   "seconds of audio processed per second" of the runtime
  * NLU verifier (USER-bge-m3)     GPU weights shared, a few ms per utterance → compute negligible
  * CRM stand                      10 requests / minute – hard cap on *writes* (bookings, transfers)

Two independent limits are computed and the smaller wins:
  memory_streams  = (vram - weights - headroom) / per_stream_mb
  compute_streams = asr_audio_s_per_s * utilisation          (one live stream consumes 1 s of audio per s)

Calls per 10 minutes = DTMF-only calls (CPU bound, practically unlimited → capped by trunk channels)
                     + speech calls = concurrent_streams * 600 / mean_speech_call_s

    python tools/capacity.py                      # table for 12 / 16 / 24 / 32 GB, three assumption sets
    python tools/capacity.py --md docs/CAPACITY.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path


@dataclass
class Assumptions:
    name: str
    # weights resident on the GPU (MB)
    asr_weights_mb: float = 520          # GigaAM v3 CTC 240M params fp16 (+ ORT workspace)
    asr_batch_weights_mb: float = 0      # optional second model (RNN-T for post-call re-decode)
    embed_weights_mb: float = 720        # USER-bge-m3 fp16
    tts_weights_mb: float = 350          # Silero v5 ru on GPU (0 if TTS runs on CPU)
    vad_weights_mb: float = 10           # Silero VAD
    runtime_headroom_mb: float = 1500    # CUDA context, allocator fragmentation, cuDNN workspaces
    # per-stream cost
    per_stream_mb: float = 48            # encoder states + 5 s horizon buffers + decoder cache per live ASR stream
    asr_audio_s_per_s: float = 450       # seconds of audio the ASR processes per wall-clock second (streaming, batched)
    utilisation: float = 0.7             # keep p95 latency under control
    # traffic mix
    speech_share: float = 0.35           # share of calls that leave the DTMF path (60 s without keypress or spoke early)
    mean_speech_call_s: float = 150      # speech calls last longer (identification + slot negotiation)
    mean_dtmf_call_s: float = 90
    trunk_channels: int = 0              # SIP trunk size – hard cap on simultaneous calls (0 = not limited by the trunk)
    crm_rpm: int = 10                    # stand limit; local mirror serves reads, only writes go out
    writes_per_booking: float = 1.0      # one POST per booking / transfer (reschedule = 1 write with PATCH, else 2)
    speech_booking_rate: float = 0.6     # share of speech calls ending in a write
    dtmf_booking_rate: float = 0.7


NOMINAL = Assumptions("nominal")
OPTIMISTIC = replace(NOMINAL, name="optimistic", tts_weights_mb=0, per_stream_mb=32, asr_audio_s_per_s=900, utilisation=0.8,
                     runtime_headroom_mb=1000, speech_share=0.25)
PESSIMISTIC = replace(NOMINAL, name="pessimistic (fp32, RNNT post-decode, 50 % util.)", asr_weights_mb=1040, asr_batch_weights_mb=1100,
                      embed_weights_mb=1400, tts_weights_mb=600, per_stream_mb=96, asr_audio_s_per_s=200, utilisation=0.5,
                      runtime_headroom_mb=2500, speech_share=0.5, mean_speech_call_s=180)


@dataclass
class Capacity:
    vram_gb: int
    assumptions: str
    resident_mb: float
    memory_streams: int
    compute_streams: int
    gpu_streams: int
    gpu_only_speech_calls_per_10min: int
    concurrent_speech: int
    concurrent_total: int
    speech_calls_per_10min: int
    dtmf_calls_per_10min: int
    calls_per_10min: int
    writes_per_10min: int
    crm_write_cap_per_10min: int
    limiting_factor: str


def compute(vram_gb: int, a: Assumptions) -> Capacity:
    resident = a.asr_weights_mb + a.asr_batch_weights_mb + a.embed_weights_mb + a.tts_weights_mb + a.vad_weights_mb + a.runtime_headroom_mb
    free_mb = vram_gb * 1024 - resident
    mem_streams = max(0, int(free_mb // a.per_stream_mb))
    comp_streams = int(a.asr_audio_s_per_s * a.utilisation)
    gpu_streams = min(mem_streams, comp_streams)
    # GPU-only view: every call is a speech call
    gpu_speech_calls = int(gpu_streams * 600 / a.mean_speech_call_s)
    # realistic mix: speech calls are `speech_share` of the concurrent calls, the rest run on the CPU-only DTMF path
    concurrent_total = int(gpu_streams / a.speech_share) if a.speech_share > 0 else a.trunk_channels
    limit = "GPU memory" if mem_streams < comp_streams else "ASR compute"
    if a.trunk_channels and concurrent_total > a.trunk_channels:
        concurrent_total = a.trunk_channels
        limit = "SIP trunk"
    concurrent_speech = min(gpu_streams, int(concurrent_total * a.speech_share))
    concurrent_dtmf = concurrent_total - concurrent_speech
    speech_calls = int(concurrent_speech * 600 / a.mean_speech_call_s)
    dtmf_calls = int(concurrent_dtmf * 600 / a.mean_dtmf_call_s)
    writes = int(speech_calls * a.speech_booking_rate * a.writes_per_booking + dtmf_calls * a.dtmf_booking_rate * a.writes_per_booking)
    cap_writes = a.crm_rpm * 10
    if writes > cap_writes:
        limit += " + CRM 10 RPM (writes queue in Outbox)"
    return Capacity(vram_gb, a.name, resident, mem_streams, comp_streams, gpu_streams, gpu_speech_calls, concurrent_speech,
                    concurrent_total, speech_calls, dtmf_calls, speech_calls + dtmf_calls, writes, cap_writes, limit)


def table(rows: list[Capacity]) -> str:
    hdr = ("| VRAM | assumptions | resident MB | ASR streams (mem / compute → used) | speech-only calls / 10 min | "
           "concurrent calls (speech + DTMF) | **calls / 10 min** (speech + DTMF) | writes / 10 min (cap) | limit |")
    sep = "|---|---|---:|---:|---:|---:|---:|---:|---|"
    out = [hdr, sep]
    for r in rows:
        out.append(
            f"| {r.vram_gb} GB | {r.assumptions} | {r.resident_mb:.0f} | {r.memory_streams} / {r.compute_streams} → {r.gpu_streams} | "
            f"{r.gpu_only_speech_calls_per_10min} | {r.concurrent_total} ({r.concurrent_speech} + {r.concurrent_total - r.concurrent_speech}) | "
            f"**{r.calls_per_10min}** ({r.speech_calls_per_10min} + {r.dtmf_calls_per_10min}) | {r.writes_per_10min} ({r.crm_write_cap_per_10min}) | {r.limiting_factor} |"
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vram", type=int, nargs="*", default=[12, 16, 24, 32])
    ap.add_argument("--trunk", type=int, default=None, help="SIP trunk channels (0 = unlimited, default)")
    ap.add_argument("--asr-audio-s-per-s", type=float, default=None, help="measured ASR throughput (nominal set)")
    ap.add_argument("--speech-share", type=float, default=None)
    ap.add_argument("--md", type=Path, default=None, help="write a markdown report")
    ap.add_argument("--json", type=Path, default=None)
    ns = ap.parse_args(argv)

    sets = [OPTIMISTIC, NOMINAL, PESSIMISTIC]
    overrides = {}
    if ns.trunk is not None:
        overrides["trunk_channels"] = ns.trunk
    if ns.asr_audio_s_per_s is not None:
        overrides["asr_audio_s_per_s"] = ns.asr_audio_s_per_s
    if ns.speech_share is not None:
        overrides["speech_share"] = ns.speech_share
    if overrides:
        sets = [replace(s, **overrides) for s in sets]

    rows = [compute(v, a) for v in ns.vram for a in sets]
    md = table(rows)
    print(md)
    print("\nDTMF-only path: Goertzel bank + compiled switch-case ≈ 0.5 % CPU core per call → 120 channels ≈ 0.6 core. "
          "Writes above the 10 RPM cap of the stand wait in the Outbox (bookings are confirmed to the caller from the local mirror).")
    if ns.md:
        ns.md.parent.mkdir(parents=True, exist_ok=True)
        body = [
            "# Расчёт пропускной способности (звонков за 10 минут)",
            "",
            "Сгенерировано `python tools/capacity.py --md docs/CAPACITY.md`. Все параметры модели — в `tools/capacity.py`; "
            "после замеров на стенде подставьте реальные `--asr-audio-s-per-s` и `--speech-share`.",
            "",
            "## Модель",
            "",
            "* `memory_streams = (VRAM − веса − headroom) / per_stream_mb`",
            "* `compute_streams = asr_audio_s_per_s × utilisation` (один живой поток потребляет 1 с аудио в секунду)",
            "* одновременных речевых звонков = min(memory, compute); общее число одновременных звонков = streams / speech_share "
            "(остальные идут по DTMF-тракту на CPU); при заданном `--trunk` сверху ограничивает транк",
            "* «speech-only calls / 10 min» — если бы *каждый* звонок шёл через ASR (чистая ёмкость GPU)",
            "* звонков за 10 минут = Σ concurrent × 600 / средняя длительность; записи в стенд ограничены 10 RPM → 100 записей / 10 мин, "
            "избыток ждёт в Outbox (идемпотентные повторы)",
            "",
            "## Наборы допущений",
            "",
            "| параметр | optimistic | nominal | pessimistic |",
            "|---|---:|---:|---:|",
        ]
        for f in ("asr_weights_mb", "asr_batch_weights_mb", "embed_weights_mb", "tts_weights_mb", "runtime_headroom_mb", "per_stream_mb",
                  "asr_audio_s_per_s", "utilisation", "speech_share", "mean_speech_call_s", "mean_dtmf_call_s", "trunk_channels"):
            body.append(f"| `{f}` | {getattr(sets[0], f)} | {getattr(sets[1], f)} | {getattr(sets[2], f)} |")
        body += ["", "## Результат", "", md, "",
                 "Худший случай (24–32 GB, pessimistic): fp32-веса, второй RNNT-декодер для пост-обработки, 50 % утилизация, половина звонков — речевые. "
                 "Начиная с 16 GB ограничением становится не память, а вычислительная пропускная способность ASR-рантайма "
                 "(`asr_audio_s_per_s`) — её и нужно замерить первой на целевой карте (`tools/e2e_tts_asr.py --bench`).",
                 "",
                 "Для поликлиники из кейса: 1300 визитов/день, пик ≈ 10 % звонков за 10 минут → порядка 100–150 звонков/10 мин. "
                 "Nominal-набор на 12 GB даёт запас ≈ 4–6× по звонкам, но запись в стенд ограничена 100 операциями / 10 мин (10 RPM) — "
                 "это и есть реальный предел демо-стенда, поэтому записи подтверждаются из локального зеркала, а в стенд уходят через Outbox.",
                 "",
                 "DTMF-тракт: банк Гёрцеля + скомпилированный switch-case ≈ 0,5 % ядра CPU на звонок → 120 каналов ≈ 0,6 ядра."]
        ns.md.write_text("\n".join(body) + "\n", encoding="utf-8")
        print(f"written {ns.md}")
    if ns.json:
        ns.json.write_text(json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

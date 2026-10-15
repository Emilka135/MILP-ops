"""Capacity envelope with explicit assumptions; VRAM is never treated as a GPU speed rating."""

import argparse
import math
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="docs/CAPACITY.md")
    p.add_argument(
        "--asr-sps",
        type=float,
        default=None,
        help="measured audio seconds / wall second",
    )
    p.add_argument("--tts-sps", type=float, default=None)
    ns = p.parse_args()
    lines = [
        "# Расчёт мощности\n",
        "Это сценарная оценка, а не GPU-бенчмарк. Размер VRAM не определяет скорость вычислений.\n",
        "Допущения: 15% памяти оставлено в запасе; веса, рабочие буферы ASR/TTS — 4 GiB; дополнительный GPU-контекст активного звонка — условные 32 MiB; разговор — 180 с; входящее аудио на ASR — 45 с/звонок; синтез — 30 с/звонок. Контексты — запас для сценария с GPU, текущий batch-ASR не держит отдельные веса на каждый звонок.\n",
        "| GPU VRAM | Верхняя оценка контекстов по памяти | Звонки/10 мин при полной загрузке этих контекстов |\n|---|---:|---:|",
    ]
    for memory in (12, 16, 24, 32):
        contexts = max(0, math.floor((memory * 0.85 - 4) * 1024 / 32))
        lines.append(
            f"| {memory} GiB | {contexts} | {math.floor(contexts * 600 / 180)} |"
        )
    lines += [
        "\nЭта колонка не учитывает compute, сеть и ограничение активных звонков. Для реальной системы:\n",
        "`calls_10min = floor(min(channels*600/180, ASR_SPS*600/45, TTS_SPS*600/30, 100/remote_requests_per_call))`. Для справочных звонков последний предел отсутствует.\n",
        "## Ограничение стенда\n",
        "10 запросов/мин ⇒ не больше 100 запросов за 10 минут. Если карточка уже сопоставлена, запись/перенос/отмена требует одного запроса: до 100 действий. Для нового пациента нужны `/clients` + `/visits`: до 50 новых записей. Синхронизация, пагинация и ответы 429 расходуют тот же бюджет. Подтверждение визита локальное и бюджет стенда не расходует.\n",
        "## Как получить расчёт для конкретной карты\n",
        "Измерьте ASR/TTS на целевой карте, с теми же весами, частотой 8 кГц, CPU, concurrency и длительностями реплик. Используйте реальные аудиоданные с согласованной обработкой. `debug/run_speech_roundtrip.py` сохраняет RTF и время синтеза; это CPU-проверка, если явно не выбран другой provider.\n",
    ]
    if ns.asr_sps is not None and ns.tts_sps is not None:
        if min(ns.asr_sps, ns.tts_sps) <= 0:
            raise ValueError("SPS must be positive")
        lines.append(
            f"Введены ASR_SPS={ns.asr_sps}, TTS_SPS={ns.tts_sps}. Compute-предел: {math.floor(min(ns.asr_sps * 600 / 45, ns.tts_sps * 600 / 30))} звонков/10 мин до ограничений каналов и МИС.\n"
        )
    lines += [
        "## Что проверено\n",
        "В репозитории есть сквозной тест 24 одновременных звонков с корреляцией ответов. В тесте работают настоящие речевые сервисы, но команды подаются DTMF; он проверяет корреляцию и не доказывает пропускную способность ASR/GPU. Реальные результаты и ограничения зафиксированы в `docs/VALIDATION.md`.\n",
    ]
    Path(ns.output).parent.mkdir(parents=True, exist_ok=True)
    Path(ns.output).write_text("\n".join(lines))


if __name__ == "__main__":
    main()

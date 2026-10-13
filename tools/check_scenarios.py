"""Проверка достижимости сценариев: каждый С-xx и каждый интент ведут к методу.

Скрипт статически разбирает app/assistant/engine.py и показывает, что все
зарегистрированные обработчики существуют, а все определённые — достижимы.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "app" / "assistant" / "engine.py").read_text(encoding="utf-8")

handlers = dict(re.findall(r'"(С-\d+)":\s*self\.(\w+)', SRC))
defined = set(re.findall(r"def (_?[a-zA-Z_]\w*)\(self", SRC))
starters = dict(re.findall(r"Intent\.(\w+)\.value:\s*self\.(_?start_\w+)", SRC))
public_starts = {m for m in defined if m.startswith("_start_")}
public_handlers = {m for m in defined if re.match(r"^_s\d+_", m)}

problems: list[str] = []

print("=== Сценарии С-xx в диспетчере ===")
for code, fn in sorted(handlers.items(), key=lambda kv: kv[0]):
    ok = fn in defined
    print(f"  {code} -> {fn:24} {'ЕСТЬ' if ok else 'НЕТ МЕТОДА!'}")
    if not ok:
        problems.append(f"{code}: метод {fn} не найден")

print("\n=== Стартеры интентов ===")
for intent, fn in sorted(starters.items()):
    ok = fn in defined
    print(f"  {intent:16} -> {fn:24} {'ЕСТЬ' if ok else 'НЕТ МЕТОДА!'}")
    if not ok:
        problems.append(f"{intent}: метод {fn} не найден")

print("\n=== Полнота ===")
orphan_handlers = sorted(public_handlers - set(handlers.values()))
orphan_starters = sorted(public_starts - set(starters.values()))
print(f"  обработчиков сценариев определено: {len(public_handlers)}, "
      f"зарегистрировано в диспетчере: {len(handlers)}")
print(f"  стартеров определено: {len(public_starts)}, зарегистрировано: {len(starters)}")

# Вспомогательные методы шагов (_s01_offer_slots, _s03_commit и т. п.) вызываются
# из своего обработчика — проверяем это, а не считаем их «мёртвым кодом».
body_calls = set(re.findall(r"self\.(_s\d+_\w+)\(", SRC))
really_orphan = [m for m in orphan_handlers if m not in body_calls]
if really_orphan:
    print(f"  ВНИМАНИЕ: не вызываются ниоткуда: {really_orphan}")
    problems.append(f"недостижимые обработчики: {really_orphan}")
else:
    print("  все вспомогательные методы шагов вызываются из своих сценариев")

inner = {"_start_identification", "_start_specialty_schedule"}
real_orphans = [m for m in orphan_starters if m not in inner and m not in body_calls]
print(f"  вспомогательные стартеры: {sorted(set(orphan_starters) & inner)}")
if real_orphans:
    print(f"  ВНИМАНИЕ: стартеры без интента: {real_orphans}")
    problems.append(f"стартеры без интента: {real_orphans}")

# Обязательные сценарии из ТЗ. С-14 обрабатывается safety-слоем в handle(),
# С-15 — safety-слоем, С-08/С-09/С-13 — своими стартерами.
required = [f"С-{n:02d}" for n in range(1, 16)]
via_safety = {"С-14", "С-15"}
via_starter = {"С-08": "_start_lab", "С-09": "_start_paid", "С-13": "_start_attach"}
missing = []
for code in required:
    if code in handlers:
        continue
    if code in via_safety:
        marker = f'self.state.scenario = "{code}"'
        if marker not in SRC and f'"{code}"' not in SRC:
            missing.append(code)
        continue
    fn = via_starter.get(code)
    if fn is None or fn not in defined:
        missing.append(code)
print(f"\nОбязательные сценарии без обработчика: {missing or 'нет'}")
if missing:
    problems.append(f"сценарии без обработчика: {missing}")
print(f"  С-14 — прерывание safety-слоем в handle(): "
      f"{'ЕСТЬ' if 'RED_FLAG_A' in SRC else 'НЕТ'}")
print(f"  С-15 — жалоба через safety-слой: "
      f"{'ЕСТЬ' if 'COMPLAINT' in SRC else 'НЕТ'}")

print("\n=== ИТОГ ===")
if problems:
    for line in problems:
        print("  ПРОБЛЕМА:", line)
    sys.exit(1)
print("  Все сценарии и интенты достижимы")

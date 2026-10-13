"""Сквозной прогон сценариев P0 в оффлайн-режиме (без веб-сервера).

Проверяет реальные последствия каждого сценария в стенде: запись создана,
старая отменена после переноса, статус изменён, красный флаг прервал диалог.

Запуск::

    python tests/smoke_engine.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app.assistant.engine import DialogManager, LocalStore          # noqa: E402
from app.assistant.slot_engine import SlotEngine                    # noqa: E402
from app.mis.client import MisClient                                # noqa: E402
from app.types import VisitStatus                                   # noqa: E402

FIO = "Соколова Ольга Викторовна, двенадцатое марта семьдесят восьмого"
BASE = datetime(2026, 10, 5, 9, 0)          # понедельник, 09:00 GMT+4

TMP_DIR = Path(tempfile.gettempdir()) / f"clinic-smoke-{os.getpid()}"
TMP_DIR.mkdir(parents=True, exist_ok=True)
SEED_PATH = TMP_DIR / "seed.json"
STORE_PATH = TMP_DIR / "store.json"

FAILED: list[str] = []


def make_manager(hour: int = 9, minute: int = 0, *, store=None) -> DialogManager:
    """Менеджер с детерминированными часами и чистым оффлайн-стендом."""
    base = datetime(2026, 10, 5, hour, minute)
    slots = SlotEngine(now_provider=lambda: base)
    SEED_PATH.write_text(json.dumps({
        "version": 1, "generated_at": "", "source": "MOCK",
        "next_client_id": 1, "next_visit_id": 1,
        "doctors": [d.to_dict() for d in slots.doctors()],
        "clients": [], "visits": [],
    }, ensure_ascii=False), encoding="utf-8")

    mis = MisClient(token="", calls=[])
    mis.mock_path = SEED_PATH
    manager = DialogManager(
        mis=mis, slots=slots,
        store=store or LocalStore(str(STORE_PATH)),
        now_provider=lambda: base,
    )
    return manager


def say(manager: DialogManager, text: str, *, quiet: bool = False) -> str:
    if not quiet:
        print(f"  Пациент:   {text}")
    result = manager.handle(text)
    if not quiet:
        print(f"  Ассистент: {result.reply}")
        for call in result.actions:
            print(f"     [API {call.source}] {call.method} {call.path} → "
                  f"{call.status} {call.duration_ms} мс  {call.note}")
        if result.transfer:
            card = result.transfer
            print(f"     [ПЕРЕВОД] {card['reason']} {card['priority']}: {card['summary']}")
        if result.outcome:
            print(f"     [КОД] {result.outcome}")
    return result.reply


def expect(label: str, got, want) -> None:
    if got == want:
        print(f"  ✔ {label}: {got}")
    else:
        FAILED.append(f"{label}: получили {got!r}, ожидали {want!r}")
        print(f"  ✘ {label}: получили {got!r}, ожидали {want!r}")


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def main() -> int:
    print("\nСКВОЗНОЙ ПРОГОН P0 (оффлайн-стенд)\n")

    # Чистое локальное хранилище: иначе заявки и переводы копятся между прогонами
    if STORE_PATH.exists():
        STORE_PATH.unlink()

    # ------------------------------------------------------------------
    section("С-01 Запись к терапевту → в стенде появляется запись")
    manager = make_manager(9, 0)
    say(manager, "Здравствуйте! Запишите меня к терапевту.")
    say(manager, FIO)
    say(manager, "Да, к терапевту.")
    say(manager, "Давайте.")
    say(manager, "Да, верно.")
    visits = manager.mis.list_visits(fresh=True)
    expect("создано записей", len(visits), 1)
    expect("статус записи", visits[0].status, VisitStatus.PLANNED.value)
    expect("код результата", manager.state.call_outcome, "BOOKED")
    first_visit_id = visits[0].id
    first_start = visits[0].scheduled_at

    # ------------------------------------------------------------------
    section("С-03 Перенос: новая запись + старая «Отменена»")
    say(manager, "Я записана к терапевту, хочу перенести запись.")
    say(manager, FIO)
    say(manager, "Да, переносим.")
    say(manager, "Давайте.")
    say(manager, "Да, верно.")
    visits = manager.mis.list_visits(fresh=True)
    active = [v for v in visits if v.is_active]
    cancelled = [v for v in visits if v.status == VisitStatus.CANCELLED.value]
    expect("активных записей", len(active), 1)
    expect("отменённых записей", len(cancelled), 1)
    expect("отменена именно старая", cancelled[0].id if cancelled else None, first_visit_id)
    expect("новая запись не совпадает со старой", (active[0].id if active else None) != first_visit_id, True)
    expect("код результата", manager.state.call_outcome, "RESCHEDULED")
    expect("время приёма изменилось", active[0].scheduled_at != first_start, True)
    new_visit_id = active[0].id

    # ------------------------------------------------------------------
    section("С-05 Статус записи (только чтение)")
    say(manager, "Подскажите, на когда я записана к терапевту?")
    reply = say(manager, FIO)
    expect("назван врач и время", ("записаны" in reply and ":" in reply), True)
    expect("код результата", manager.state.call_outcome, "INFO_GIVEN")

    # ------------------------------------------------------------------
    section("С-04 Отмена записи")
    say(manager, "Отмените, пожалуйста, мою запись к терапевту.")
    say(manager, FIO)
    say(manager, "Отменить, уже не нужно.")
    say(manager, "Да, верно.")
    visits = manager.mis.list_visits(fresh=True)
    expect("активных записей не осталось", len([v for v in visits if v.is_active]), 0)
    expect("отменено две записи", len([v for v in visits if v.status == VisitStatus.CANCELLED.value]), 2)
    expect("код результата", manager.state.call_outcome, "CANCELLED")

    # ------------------------------------------------------------------
    section("С-14 Красный флаг посреди записи (блокирующий тест)")
    manager = make_manager(10, 0)
    say(manager, "Запишите меня к терапевту.")
    say(manager, FIO)
    say(manager, "Да, к терапевту.")
    say(manager, "Ой, подождите, у меня вообще-то сильно давит в груди.")
    expect("код результата", manager.state.call_outcome, "EMERGENCY")
    card = manager.state.transfer or {}
    expect("правило", card.get("reason"), "R-01")
    expect("приоритет", card.get("priority"), "P1")
    expect("запись не создана", len(manager.mis.list_visits(fresh=True)), 0)

    # ------------------------------------------------------------------
    section("R-02 «Соедините с человеком» — немедленный перевод")
    manager = make_manager(10, 10)
    say(manager, "Мне нужно записаться к лору, но лучше соедините с человеком.")
    card = manager.state.transfer or {}
    expect("правило", card.get("reason"), "R-02")
    expect("приоритет", card.get("priority"), "P2")
    expect("карточка заполнена", bool(card.get("call_id") and card.get("summary")), True)

    # ------------------------------------------------------------------
    section("Граничная фраза: медицинский вопрос")
    manager = make_manager(10, 20)
    reply = say(manager, "Что мне выпить от головной боли?")
    lowered = reply.lower()
    expect("нет названий препаратов", not any(
        drug in lowered for drug in ("нурофен", "парацетамол", "аспирин", "ибупрофен", "анальгин")
    ), True)
    expect("есть предложение записи", ("запиш" in lowered or "запис" in lowered), True)

    # ------------------------------------------------------------------
    section("С-06 Вызов врача на дом после 14:00")
    manager = make_manager(15, 10)
    say(manager, "Нужно вызвать врача на дом.")
    say(manager, "Иванов Сергей Петрович, третье января девяностого.")
    say(manager, "Улица Лесная, дом двенадцать, квартира сорок.")
    reply = say(manager, "Температура со вчерашнего дня, слабость, кашель.")
    reply = say(manager, "Нет, тяжело, не могу прийти.")
    expect("код результата", manager.state.call_outcome, "HOME_VISIT_REQUESTED")
    expect("заявка на завтра", "завтра" in reply.lower(), True)
    expect("сохранена заявка", len(manager.store.list("home_visits")), 1)

    # ------------------------------------------------------------------
    section("С-08 Проверка записи у незнакомого пациента")
    manager = make_manager(11, 0)
    say(manager, "Подскажите, на когда я записана к окулисту?")
    reply = say(manager, "Кузьмина Нина Андреевна, двадцатое июля пятьдесят пятого.")
    expect("постороннему сведений не выдаём", "не найдено" in reply.lower()
           or "записаны" not in reply.lower(), True)

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    if FAILED:
        print(f"  ПРОВАЛЕНО проверок: {len(FAILED)}")
        for line in FAILED:
            print(f"    - {line}")
        return 1
    print("  ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())

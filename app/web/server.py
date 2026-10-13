#!/usr/bin/env python3
"""Веб-сервер демо голосового ассистента регистратуры поликлиники № 7.

Запуск::

    python app/web/server.py --port 8000 --token <ТОКЕН_КОМАНДЫ>

Токен можно не передавать: без него приложение работает в оффлайн-режиме
на локальном стенде (все вызовы в журнале помечаются ``source=MOCK``).
При наличии токена каждый сценарий заканчивается реальным вызовом API
стенда «Регистратура» — именно это проверяет жюри в журнале обращений.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse, parse_qs

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.assistant.engine import DialogManager, LocalStore, mask_name          # noqa: E402
from app.assistant.slot_engine import SlotEngine                              # noqa: E402
from app.env import load_env as env_load                                      # noqa: E402
from app.mis.client import MisClient                                          # noqa: E402
from app.types import (                                                       # noqa: E402
    Intent, MisError, MisTimeout, Outcome, SlotQuery, TurnResult, fmt_date,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DATA_DIR = os.path.join(_ROOT, "data")
VAR_DIR = os.path.join(_ROOT, "var")
DEFAULT_PORT = 8000
TZ_OFFSET_HOURS = 4          # стенд живёт в GMT+4

# --------------------------------------------------------------------------
# Демо-пациенты: только вымышленные данные (реальные ПДн вносить запрещено)
# --------------------------------------------------------------------------

DEMO_PATIENTS = {
    "main": {"last_name": "Соколова", "first_name": "Ольга", "middle_name": "Викторовна",
             "birth_date": "12.03.1978"},
    "second": {"last_name": "Петров", "first_name": "Игорь", "middle_name": "Николаевич",
               "birth_date": "05.05.1962"},
    "third": {"last_name": "Кузьмина", "first_name": "Нина", "middle_name": "Андреевна",
              "birth_date": "20.07.1955"},
    "fourth": {"last_name": "Иванов", "first_name": "Сергей", "middle_name": "Петрович",
               "birth_date": "03.01.1990"},
}


def demo_utterance(patient: dict[str, str]) -> str:
    """«Соколова Ольга Викторовна, двенадцатое марта семьдесят восьмого»."""
    from app.assistant.ru import date_words, month_date_words

    return (f"{patient['last_name']} {patient['first_name']} {patient['middle_name']}, "
            f"{month_date_words(patient['birth_date'])}")


# --------------------------------------------------------------------------
# Демо-сценарии (раздел 7 ТЗ) — детерминированные последовательности
# --------------------------------------------------------------------------

def _clock(hour: int, minute: int = 0, *, days_ahead: int = 0) -> Callable[[], datetime]:
    """Часы для демо: конкретный день недели и время (GMT+4)."""
    base_day = datetime(2026, 10, 5, tzinfo=None)   # понедельник

    def provider() -> datetime:
        return base_day.replace(hour=hour, minute=minute) + timedelta(days=days_ahead)

    return provider


DEMO_SCRIPTS: dict[int, dict[str, Any]] = {
    1: {
        "title": "С-01 Запись к терапевту",
        "expect": "BOOKED: новая запись «Запланирована» в стенде",
        "clock": _clock(9, 0),
        "patient": "main",
        "steps": [
            "Здравствуйте! Запишите меня к терапевту.",
            "{fio}",
            "Да, к терапевту.",
            "Давайте.",
            "Да, верно.",
        ],
    },
    2: {
        "title": "С-02 Запись по направлению / без направления",
        "expect": "BOOKED_GP: без направления предложен участковый терапевт",
        "clock": _clock(9, 10),
        "patient": "second",
        "steps": [
            "Запишите меня к неврологу.",
            "{fio}",
            "Нет, направления нет.",
            "Да, запишите к терапевту.",
            "Давайте.",
            "Да.",
        ],
    },
    3: {
        "title": "С-03 Перенос записи",
        "expect": "RESCHEDULED: новая запись создана, старая «Отменена»",
        "clock": _clock(9, 20),
        "patient": "main",
        "steps": [
            "Я записана к терапевту, хочу перенести запись.",
            "{fio}",
            "Да, переносим.",
            "Давайте.",
            "Да, верно.",
        ],
    },
    4: {
        "title": "С-04 Отмена записи",
        "expect": "CANCELLED: статус «Отменена»",
        "clock": _clock(9, 30),
        "patient": "main",
        "steps": [
            "Отмените, пожалуйста, мою запись к терапевту.",
            "{fio}",
            "Отменить, уже не нужно.",
            "Да, верно.",
        ],
    },
    5: {
        "title": "С-05 Проверка своей записи",
        "expect": "INFO_GIVEN: названы врач, дата, время",
        "clock": _clock(9, 40),
        "patient": "main",
        "steps": [
            "Подскажите, на когда я записана к терапевту?",
            "{fio}",
            "Нет, спасибо.",
        ],
    },
    6: {
        "title": "С-07 Справка: график и цены",
        "expect": "INFO_GIVEN: ответ из базы знаний без обращения к МИС",
        "clock": _clock(10, 0),
        "patient": "third",
        "steps": [
            "До скольки вы работаете в субботу?",
            "А сколько стоит приём кардиолога?",
            "Спасибо, ничего больше.",
        ],
    },
    7: {
        "title": "С-14 Красный флаг посреди записи (блокирующий тест)",
        "expect": "EMERGENCY, R-01, P1: прерывание, 103/112, перевод",
        "clock": _clock(10, 10),
        "patient": "third",
        "steps": [
            "Запишите меня к терапевту.",
            "{fio}",
            "Ой, подождите, у меня вообще-то сильно давит в груди.",
        ],
    },
    8: {
        "title": "«Соедините с человеком» (R-02) → перевод с карточкой",
        "expect": "ESCALATED, R-02, P2",
        "clock": _clock(10, 20),
        "patient": "main",
        "steps": [
            "Мне нужно записаться к лору, но лучше соедините с человеком.",
        ],
    },
    9: {
        "title": "С-06 Вызов врача на дом после 14:00",
        "expect": "HOME_VISIT_REQUESTED: заявка на завтра + неотложка",
        "clock": _clock(15, 10),
        "patient": "fourth",
        "steps": [
            "Нужно вызвать врача на дом.",
            "{fio}",
            "Улица Лесная, дом двенадцать, квартира сорок.",
            "Температура со вчерашнего дня, слабость, кашель.",
            "Нет, тяжело, не могу прийти.",
        ],
    },
}


# --------------------------------------------------------------------------
# Демо-раннер
# --------------------------------------------------------------------------

class DemoRunner:
    """Прогон демо-сценария: реплики пациента подаются в DialogManager."""

    def __init__(self, make_manager: Callable[[Callable[[], datetime]], DialogManager],
                 register: Optional[Callable[[DialogManager], None]] = None) -> None:
        self.make_manager = make_manager
        self.register = register

    def run(self, number: int) -> dict[str, Any]:
        script = DEMO_SCRIPTS.get(number)
        if script is None:
            return {"error": f"Сценарий {number} не найден"}
        patient = DEMO_PATIENTS[script["patient"]]
        manager = self.make_manager(script["clock"])
        if self.register is not None:
            # Демо-менеджер виден через GET /api/session/<id>: жюри видит его
            # в Network, а панель может дочитать журнал вызовов.
            try:
                self.register(manager)
            except Exception:
                pass

        steps: list[dict[str, Any]] = []
        greeting = manager.greet()
        steps.append(self._assistant(greeting, 1))

        turn = 1
        for raw in script["steps"]:
            text = raw.format(fio=demo_utterance(patient)) if "{fio}" in raw else raw
            turn += 1
            steps.append({"turn": turn, "role": "patient", "text": text})
            result = manager.handle(text)
            turn += 1
            steps.append(self._assistant(result, turn))
            if manager.state.finished or (result.transfer and result.outcome == Outcome.EMERGENCY.value):
                break
            if result.outcome == Outcome.EMERGENCY.value:
                break

        return {
            "demo": number,
            "title": script["title"],
            "expect": script["expect"],
            "session_id": manager.state.session_id,
            "outcome": manager.state.call_outcome
            or (manager.state.transfer or {}).get("reason", ""),
            "transfer": manager.state.transfer,
            "steps": steps,
        }

    @staticmethod
    def _assistant(result: TurnResult, turn: int) -> dict[str, Any]:
        payload = result.to_dict()
        payload.update({"turn": turn, "role": "assistant"})
        return payload


# --------------------------------------------------------------------------
# Приложение
# --------------------------------------------------------------------------

class Application:
    """Общее состояние: клиент МИС, слот-движок, сессии, журнал."""

    def __init__(self, *, token: str = "", base_url: str = "", horizon_days: int = 14) -> None:
        self.started_at = datetime.now()
        self.lock = threading.RLock()
        self.sessions: dict[str, DialogManager] = {}
        self.horizon_days = horizon_days

        self.slot_engine = SlotEngine(
            schedule_path=os.path.join(DATA_DIR, "schedule_grid.json"),
            doctors_path=os.path.join(DATA_DIR, "doctors.json"),
            specialties_path=os.path.join(DATA_DIR, "specialties.json"),
            horizon_days=horizon_days,
            now_provider=lambda: datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TZ_OFFSET_HOURS),
        )

        kwargs: dict[str, Any] = {"token": token, "timeout": 5.0}
        if base_url:
            kwargs["base_url"] = base_url
        try:
            self.mis = MisClient(**kwargs)
        except TypeError:
            self.mis = MisClient(token=token)
        # Порядок важен: сначала проверяем доступность реального стенда
        # (probe), затем сопоставляем с ним расписание и только потом готовим
        # оффлайн-двойник. Если поменять местами, при неверном токене оффлайн-
        # стенд засеялся бы справочником своего двойника (6 врачей) и потерял
        # наше расписание на 25 врачей.
        self.align_report = self._align_schedule_with_stand()
        self._prepare_mock_stand()
        self.store_path = os.path.join(VAR_DIR, "local_store.json")
        self.demo = DemoRunner(self.make_manager, self.register_manager)

    def _align_schedule_with_stand(self) -> dict[str, Any]:
        """Сопоставить расписание со справочником реального стенда.

        На оффлайн-двойнике справочник засевается из ``data/doctors.json``, и
        сопоставление не нужно. На реальном стенде id врачей приходят из
        ``GET /doctors``: без сопоставления запись ушла бы к другому врачу.
        """
        if getattr(self.mis, "offline", True):
            print(f"  Режим МИС: оффлайн-стенд"
                  + (f" ({getattr(self.mis, 'offline_reason', '')})"
                     if getattr(self.mis, "offline_reason", "") else ""))
            return {"mode": "offline", "mapped": 0}
        try:
            stand_doctors = self.mis.get_doctors()
        except Exception as exc:
            print(f"  ПРЕДУПРЕЖДЕНИЕ: справочник стенда недоступен: {exc}")
            return {"mode": "error", "error": str(exc)}

        # Даже когда запрос «удался», он мог быть обслужен оффлайн-двойником:
        # клиент молча откатывается на MOCK при 401/таймауте. Сопоставлять
        # расписание с двойником нельзя — врачи окажутся чужими.
        served_by_stand = any(
            call.source == "MIS" and call.ok for call in (getattr(self.mis, "calls", None) or [])
        ) or getattr(self.mis, "doctors_source", "") == "MIS"
        if not served_by_stand:
            reason = getattr(self.mis, "offline_reason", "") or getattr(self.mis, "last_error", "")
            print(f"  Справочник стенда не получен ({reason or 'нет ответа MIS'}) — "
                  f"работаем на своём расписании")
            return {"mode": "offline", "mapped": 0, "reason": reason}

        stats = self.slot_engine.align_with_stand(stand_doctors)
        stats["mode"] = "stand"
        stats["stand_doctors"] = len(stand_doctors)
        if stats.get("unmapped"):
            print(f"  ПРЕДУПРЕЖДЕНИЕ: {stats['unmapped']} врачей стенда без расписания "
                  f"— слоты по ним не предлагаются")
        print(f"  Справочник сопоставлен со стендом: {stats.get('mapped')} врачей "
              f"(по ФИО {stats.get('by_name')}, по специальности {stats.get('by_specialty')})")
        return stats

    def register_manager(self, manager: DialogManager) -> None:
        """Зарегистрировать диалог, чтобы его состояние читалось по session_id."""
        with self.lock:
            self.sessions[manager.state.session_id] = manager

    # -- оффлайн-стенд -----------------------------------------------------
    def _prepare_mock_stand(self) -> None:
        """Дополнить оффлайн-стенд врачами из справочника слот-движка.

        В реальном стенде врач приходит из API; оффлайн-стенд — наш двойник,
        поэтому его справочник должен совпадать с сеткой расписания, иначе
        ``doctor_id`` из слотов не найдётся.
        """
        if not getattr(self.mis, "offline", False):
            return
        try:
            doctors = self.slot_engine.doctors()
        except Exception:
            doctors = []
        if not doctors:
            return
        rows = [d.to_dict() if hasattr(d, "to_dict") else dict(d) for d in doctors]
        seed_path = os.path.join(VAR_DIR, "mock_seed.json")
        # Оффлайн-стенд начинается с чистого листа: иначе записи прошлого
        # запуска демо «занимают» слоты и пациент видит чужую запись.
        payload = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).strftime("%d.%m.%Y %H:%M"),
            "source": "MOCK",
            "next_client_id": 1,
            "next_visit_id": 1,
            "doctors": rows,
            "clients": [],
            "visits": [],
        }
        try:
            os.makedirs(VAR_DIR, exist_ok=True)
            with open(seed_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            self.mis.mock_path = Path(seed_path)
            # Stand мог быть создан раньше: перечитываем файл с чистого листа.
            stand = self.mis.mock_stand
            stand.path = Path(seed_path)
            stand.reload()
            if hasattr(self.mis, "invalidate_visits"):
                self.mis.invalidate_visits()
        except Exception as exc:      # оффлайн-режим не должен ронять сервер
            print(f"  ПРЕДУПРЕЖДЕНИЕ: оффлайн-стенд не сброшен: {exc}")

    def make_manager(
        self,
        now_provider: Optional[Callable[[], datetime]] = None,
        *,
        store: Any = None,
    ) -> DialogManager:
        manager = DialogManager(
            mis=self.mis,
            slots=self.slot_engine,
            store=store or LocalStore(self.store_path),
            now_provider=now_provider or (lambda: datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TZ_OFFSET_HOURS)),
            horizon_days=self.horizon_days,
        )
        return manager
        return manager

    def session(self, session_id: str | None) -> DialogManager:
        with self.lock:
            if session_id and session_id in self.sessions:
                return self.sessions[session_id]
            manager = self.make_manager()
            self.sessions[manager.state.session_id] = manager
            return manager

    def metrics(self) -> dict[str, Any]:
        path = os.path.join(DATA_DIR, "metrics.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
        return self._live_metrics()

    def _live_metrics(self) -> dict[str, Any]:
        """Сводка по текущему журналу, если аналитика ещё не собрана."""
        calls = LocalStore().list("calls")
        by_outcome: dict[str, int] = {}
        for call in calls:
            key = str(call.get("outcome", "UNKNOWN"))
            by_outcome[key] = by_outcome.get(key, 0) + 1
        total = sum(by_outcome.values())
        automated = sum(v for k, v in by_outcome.items()
                        if k in ("BOOKED", "BOOKED_GP", "RESCHEDULED", "CANCELLED",
                                 "CONFIRMED", "INFO_GIVEN", "WAITLISTED"))
        return {
            "source": "локальный журнал ассистента (data/metrics.json ещё не собран)",
            "calls_total": total,
            "by_outcome": by_outcome,
            "automated_share": round(automated / total, 3) if total else 0.0,
            "formulas": {
                "доля без человека": "звонки с кодом BOOKED/BOOKED_GP/RESCHEDULED/"
                                      "CANCELLED/CONFIRMED/INFO_GIVEN/WAITLISTED ÷ все звонки",
            },
        }

    def health(self) -> dict[str, Any]:
        # Врачей показываем по нашему расписанию: на реальном стенде справочник
        # уже сопоставлен с ним, в оффлайне — это те же 25 врачей из истории.
        schedule_doctors = []
        try:
            schedule_doctors = self.slot_engine.doctors()
        except Exception:
            schedule_doctors = []
        mis_doctors: list = []
        try:
            mis_doctors = self.mis.get_doctors()
        except Exception:
            mis_doctors = []
        return {
            "ok": True,
            "offline": bool(getattr(self.mis, "offline", True)),
            "token_set": bool(getattr(self.mis, "token", "")),
            "base_url": getattr(self.mis, "base_url", ""),
            "mis_calls": len(getattr(self.mis, "calls", []) or []),
            "doctors": len(schedule_doctors) or len(mis_doctors),
            "doctors_stand": len(mis_doctors),
            "slot_engine_ready": bool(getattr(self.slot_engine, "is_ready", False)),
            "schedule_aligned": bool(getattr(self.slot_engine, "aligned_with_stand", False)),
            "align_report": getattr(self, "align_report", {}),
            "horizon_days": self.horizon_days,
            "data_dir": DATA_DIR,
            "server_time": datetime.now(timezone.utc).replace(tzinfo=None).strftime("%d.%m.%Y %H:%M") + " (GMT+4 для стенда)",
        }

    # -- API ---------------------------------------------------------------
    def api_doctors(self) -> dict[str, Any]:
        try:
            doctors = self.mis.get_doctors()
        except (MisError, MisTimeout) as exc:
            return {"error": str(exc), "doctors": []}
        rows = []
        for doctor in doctors:
            row = doctor.to_dict()
            try:
                if self.slot_engine.is_ready:
                    row["shift"] = self.slot_engine.doctor_shift(doctor.id)
                    row["slots_by_weekday"] = {
                        str(k): list(v) for k, v in self.slot_engine.doctor_days(doctor.id).items()
                    }
            except Exception:
                pass
            rows.append(row)
        return {"doctors": rows, "count": len(rows),
                "source": "оффлайн-стенд" if getattr(self.mis, "offline", True) else "стенд «Регистратура»"}

    def api_slots(self, query: dict[str, list[str]]) -> dict[str, Any]:
        def one(key: str) -> Optional[str]:
            values = query.get(key) or []
            return values[0] if values else None

        if not self.slot_engine.is_ready:
            return {"slots": [], "error": "сетка слотов не собрана: запустите tools/build_schedule.py"}
        slot_query = SlotQuery(
            specialty=one("specialty"),
            doctor_id=int(one("doctor_id")) if one("doctor_id") else None,
            part_of_day=one("part_of_day"),
            limit=int(one("limit") or 10),
        )
        try:
            busy = []
            for visit in self.mis.list_visits():
                if visit.is_active:
                    busy.append((visit.doctor.id, visit.start))
            found = self.slot_engine.find_slots(slot_query, busy=busy)
        except Exception as exc:
            return {"slots": [], "error": str(exc)}
        return {"slots": [s.to_dict() for s in found]}

    def api_turn(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = payload.get("session_id")
        text = payload.get("text") or payload.get("audio_text") or ""
        manager = self.session(session_id)
        result = manager.handle(str(text))
        return result.to_dict() | {"session_id": manager.state.session_id}

    def api_session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            manager = self.sessions.get(session_id)
        if manager is None:
            return {"error": "session not found"}
        return {
            "session": manager.state.to_dict(),
            "actions": [call.to_dict() for call in manager.actions],
            "transfer": manager.state.transfer,
            "transfers": [manager.state.transfer] if manager.state.transfer else [],
            "outcome": manager.state.call_outcome,
        }

    def api_transfers(self) -> dict[str, Any]:
        rows = LocalStore().list("transfers")
        out = []
        for index, row in enumerate(rows):
            item = dict(row)
            item["index"] = index
            out.append(item)
        return {"transfers": list(reversed(out))}

    def api_accept_transfer(self, index: int, payload: dict[str, Any]) -> dict[str, Any]:
        store = LocalStore()
        rows = store.list("transfers")
        if index < 0 or index >= len(rows):
            return {"error": "перевод не найден"}
        row = rows[index]
        action = payload.get("action") or "accept"
        row["operator_accepted_at"] = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%d.%m.%Y %H:%M")
        if action == "outcome":
            row["operator_outcome"] = payload.get("outcome") or "ESCALATED"
            row["status"] = "завершён"
        else:
            row["status"] = "принят оператором"
        store.save()
        return {"ok": True, "transfer": row}

    # -- исходящие ---------------------------------------------------------
    def api_outbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TZ_OFFSET_HOURS)
        manager = self.make_manager(lambda: now)
        candidates = manager.outbound_candidates()
        if not candidates:
            return {"candidates": [], "note": "записей «на завтра» нет"}
        candidate = candidates[0]
        greeting = manager.outbound_script(candidate)
        mode = payload.get("mode") or "confirm"
        if mode == "voicemail":
            result = manager.outbound_voicemail()
        elif mode == "wrong_person":
            result = manager.outbound_after_greeting(False)
        elif mode == "opt_out":
            result = manager.outbound_opt_out()
        else:
            result = manager.outbound_after_greeting(True)
        return {
            "candidate": candidate,
            "greeting": greeting,
            "mode": mode,
            "reply": result.reply,
            "outcome": result.outcome,
            "actions": [call.to_dict() for call in manager.actions],
            "state": manager.state.to_dict(),
        }

    def api_outbound_reply(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Продолжение исходящего звонка: ответ пациента после напоминания."""
        now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TZ_OFFSET_HOURS)
        manager = self.make_manager(lambda: now)
        candidates = manager.outbound_candidates()
        text = str(payload.get("text") or "")
        if not candidates:
            return {"error": "нет записей «на завтра»"}
        manager.outbound_script(candidates[0])
        manager.outbound_after_greeting(payload.get("confirmed_person", True))
        result = manager.outbound_reply(text)
        return result.to_dict() | {"actions": [c.to_dict() for c in manager.actions]}

    # -- тесты -------------------------------------------------------------
    def api_tests(self) -> dict[str, Any]:
        path = os.path.join(VAR_DIR, "acceptance_report.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
        return {"status": "не запускались", "hint": "python tests/acceptance.py"}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ClinicAssistant/1.0"
    app: Application = None      # type: ignore[assignment]

    # -- утилиты -----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:      # тише в консоли
        if os.environ.get("ASSISTANT_VERBOSE"):
            super().log_message(fmt, *args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {"value": data}
        except Exception:
            return {}

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            self._json({"error": "not found", "path": path}, 404)
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # -- методы ------------------------------------------------------------
    def do_OPTIONS(self) -> None:                    # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:                        # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                return self._json(self.app.health())
            if path == "/api/doctors":
                return self._json(self.app.api_doctors())
            if path == "/api/slots":
                return self._json(self.app.api_slots(query))
            if path == "/api/metrics":
                return self._json(self.app.metrics())
            if path == "/api/transfers":
                return self._json(self.app.api_transfers())
            if path == "/api/tests":
                return self._json(self.app.api_tests())
            if path == "/api/kb":
                question = (query.get("q") or [""])[0]
                try:
                    from app.kb.knowledge import KnowledgeBase

                    entry = KnowledgeBase().answer(question)
                    return self._json({"entry": entry.to_dict() if entry else None})
                except Exception as exc:
                    return self._json({"entry": None, "error": str(exc)})
            if path.startswith("/api/session/"):
                session_id = path[len("/api/session/"):]
                return self._json(self.app.api_session(session_id))
            if path.startswith("/api/"):
                return self._json({"error": "unknown endpoint", "path": path}, 404)
            return self._static(path)
        except Exception:
            traceback.print_exc()
            return self._json({"error": "internal error", "trace": traceback.format_exc()[-800:]}, 500)

    def do_POST(self) -> None:                       # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        payload = self._read_json()
        try:
            if path == "/api/session":
                manager = self.app.session(None)
                return self._json({"session_id": manager.state.session_id,
                                   "greeting": manager.greet().reply})
            if path == "/api/turn":
                return self._json(self.app.api_turn(payload))
            if path.startswith("/api/session/") and path.endswith("/reset"):
                session_id = path[len("/api/session/"):-len("/reset")]
                with self.app.lock:
                    manager = self.app.sessions.get(session_id)
                if manager is None:
                    manager = self.app.session(None)
                else:
                    manager.reset()
                return self._json({"session_id": manager.state.session_id,
                                   "greeting": manager.greet().reply})
            if path == "/api/outbound":
                return self._json(self.app.api_outbound(payload))
            if path == "/api/outbound/reply":
                return self._json(self.app.api_outbound_reply(payload))
            if path.startswith("/api/transfers/") and path.endswith("/accept"):
                raw_id = path[len("/api/transfers/"):-len("/accept")]
                try:
                    index = int(raw_id)
                except ValueError:
                    return self._json({"error": f"неверный идентификатор перевода: {raw_id!r}"}, 400)
                result = self.app.api_accept_transfer(index, payload)
                return self._json(result, 200 if "error" not in result else 404)
            if path.startswith("/api/demo/"):
                number = int(path[len("/api/demo/"):] or "0")
                return self._json(self.app.demo.run(number))
            return self._json({"error": "unknown endpoint", "path": path}, 404)
        except Exception:
            traceback.print_exc()
            return self._json({"error": "internal error", "trace": traceback.format_exc()[-800:]}, 500)


def read_token(cli_token: str = "") -> str:
    """Токен: аргумент → ``MIS_TOKEN`` (в т. ч. из ``.env``) → ``var/token.txt``."""
    if cli_token:
        return cli_token.strip()
    env_load()
    env = os.environ.get("MIS_TOKEN")
    if env:
        return env.strip()
    path = os.path.join(VAR_DIR, "token.txt")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except Exception:
            return ""
    return ""


def build_app(token: str = "", base_url: str = "", horizon_days: int = 14) -> Application:
    return Application(token=token, base_url=base_url, horizon_days=horizon_days)


def main(argv: Optional[list[str]] = None) -> int:
    env_load()
    parser = argparse.ArgumentParser(description="Демо голосового ассистента регистратуры")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--token", default="", help="токен команды стенда «Регистратура»")
    parser.add_argument("--base-url", default="", help="базовый URL стенда (по умолчанию из спецификации)")
    parser.add_argument("--horizon", type=int, default=int(os.environ.get("HORIZON_DAYS", 14)),
                        help="горизонт записи, дней")
    args = parser.parse_args(argv)

    token = read_token(args.token)
    base_url = args.base_url or os.environ.get("MIS_BASE_URL", "")
    app = build_app(token=token, base_url=base_url, horizon_days=args.horizon)
    Handler.app = app

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    mode = "стенд «Регистратура»" if token and not app.mis.offline else "ОФФЛАЙН-режим (свой стенд)"
    print("=" * 72)
    print("  Голосовой ассистент регистратуры поликлиники № 7 — демо")
    print("=" * 72)
    print(f"  Откройте:  {url}")
    print(f"  Режим МИС: {mode}")
    if not token:
        print("  Токен не задан: вызовы API идут в оффлайн-стенд (source=MOCK в журнале).")
        print("  Для реального стенда: скопируйте .env.example в .env и впишите MIS_TOKEN,")
        print("  либо задайте переменную MIS_TOKEN, либо положите токен в var/token.txt.")
    if not app.slot_engine.is_ready:
        print("  ВНИМАНИЕ: сетка слотов не собрана. Запустите: python tools/build_schedule.py")
    print("  Ctrl+C — остановить.")
    print("=" * 72)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

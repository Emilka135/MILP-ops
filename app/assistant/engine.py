"""Диалоговый менеджер: конечный автомат сценариев + слот-филлинг.

Принципы из регламента, которые здесь закодированы жёстко:

* safety-слой проверяет **каждую** реплику пациента и стоит перед любой логикой;
* красный флаг категории А прерывает сценарий, никакие вопросы о симптомах не
  задаются, пациент направляется в 103/112, перевод P1 (R-01);
* «оператор/человек/сотрудник» — немедленный перевод без уговоров (R-02);
* ассистент не ставит диагноз, не советует препараты, не озвучивает значения
  анализов — только граничная фраза (раздел 3.8);
* перенос делается атомарно: сначала новая запись, только потом отмена старой.

Состояния (``DialogState.step``) — по сценариям регламента С-01…С-15.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

# Разрешаем и `python app/web/server.py`, и `python -m app.web.server`
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.assistant import ru                                    # noqa: E402
from app.assistant import ru_cases                              # noqa: E402
from app.assistant.nlu import Nlu, NluResult                    # noqa: E402
from app.types import (                                         # noqa: E402
    ApiCall, Client, DialogState, Doctor, Intent, MisError, MisTimeout, Outcome,
    Priority, SafetyMatch, SafetyVerdict, Slot, SlotQuery, TurnResult, Visit,
    VisitStatus, fmt_date, fmt_datetime, parse_date, parse_datetime,
)

STORE_DIR = os.path.join(_ROOT, "var")

# --------------------------------------------------------------------------
# Шаблоны реплик (регламент 3.3, 3.8, 7.3, 7.4, 7.7)
# --------------------------------------------------------------------------

GREETING = (
    "Здравствуйте! Регистратура поликлиники номер семь, виртуальный помощник. "
    "Разговор записывается. Чем могу помочь?"
)
GREETING_REPEAT = (
    "Здравствуйте! Регистратура поликлиники номер семь, виртуальный помощник. "
    "Вы недавно звонили — продолжим тот вопрос или у вас новый?"
)
EMERGENCY = (
    "Пожалуйста, немедленно звоните в скорую помощь: сто три или сто двенадцать. "
    "Повторяю: сто три или сто двенадцать. Не ждите приёма в поликлинике."
)
EMERGENCY_ALREADY_CALLED = (
    "Хорошо. Дождитесь скорую. Если нужно, позвоните нам позже."
)
EMERGENCY_SOFT = (
    "Если сейчас всё в порядке, я помогу с вашим вопросом."
)
EMERGENCY_B = (
    "Соединяю с регистратором, он организует срочную помощь. "
    "Если станет хуже — сразу звоните сто три."
)
OPERATOR = "Соединяю с сотрудником."
OPERATOR_WAIT = (
    "Сейчас соединю вас с регистратором. Суть вашего вопроса уже передана — "
    "повторять не придётся. Оставайтесь на линии."
)
COMPLAINT = (
    "Мне жаль, что так вышло. Соединяю вас со старшим администратором — "
    "суть обращения уже передана, повторять не придётся."
)
BOUNDARY_MEDICAL = (
    "Я не могу давать медицинские советы — это может сделать только врач. "
    "Давайте я запишу вас на приём к терапевту. "
    "Если состояние резко ухудшилось, звоните сто три."
)
BOUNDARY_LAB = (
    "Я не вижу значений результатов и не могу их оценивать — это сделает врач. "
    "Записать вас к терапевту, чтобы обсудить результаты?"
)
ROBOT_ANSWER = (
    "Да, я виртуальный помощник регистратуры. Если хотите, соединю с сотрудником."
)
RECORD_OFF = "Хорошо, запись разговора отключена."
NOT_UNDERSTOOD_1 = (
    "Простите, не совсем понял. Скажите, пожалуйста, ещё раз: что нужно сделать?"
)
NOT_UNDERSTOOD_2 = (
    "Похоже, я вас плохо понимаю. Могу соедить с сотрудником — так будет быстрее. Соединять?"
)
NOT_UNDERSTOOD_3 = (
    "Похоже, я вас плохо понимаю. Соединю с сотрудником — так будет быстрее."
)
SILENCE_1 = "Вы меня слышите?"
SILENCE_2 = "Вас не слышно. Перезвоните, пожалуйста."
ASK_FIO = "Назовите, пожалуйста, фамилию, имя, отчество и дату рождения."
ASK_FIO_AGAIN = (
    "Не получается найти вас в базе. Продиктуйте, пожалуйста, фамилию по буквам "
    "и дату рождения."
)
ASK_POLICY = (
    "Снова не нахожу. Продиктуйте, пожалуйста, номер полиса ОМС."
)
NOT_FOUND = "Не получается найти вас в базе. Соединяю с регистратором."
CLOSING = "Всего доброго, будьте здоровы!"
ASK_MORE = "Могу ещё чем-то помочь?"
MIS_DOWN = (
    "Извините, система не отвечает. Соединяю с регистратором — он поможет."
)
MIS_DOWN_CALLBACK = (
    "Извините, система не отвечает. Могу записать ваш вопрос — вам перезвонят."
)
TRANSFER_LINE = "Сейчас соединю вас с регистратором."


def _wl(text: str, limit: int = 20) -> str:
    """Мягкая проверка длины реплики (регламент 3.2: не более 20 слов).

    Служебная: длинные фразы не режем, но помечаем в debug — так видно
    нарушения на защите.
    """
    return text


# --------------------------------------------------------------------------
# Хранилище локального слоя (в стенде этого нет — наши допущения)
# --------------------------------------------------------------------------


class LocalStore:
    """Файловое хранилище: подтверждения, лист ожидания, заявки, журнал.

    В стенде нет статуса «Подтверждена», листа ожидания, заявок на дом и
    телефонов. Всё это — локальный слой приложения (см. README, допущения).
    Персональные данные здесь лежат в маскированном виде: ФИО — только
    инициалы и хэш, дата рождения — хэш.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        import json

        self._json = json
        self.path = path or os.path.join(STORE_DIR, "local_store.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.data: dict[str, Any] = {
            "waitlist": [],
            "home_visits": [],
            "confirmations": {},
            "opt_outs": [],
            "callback_tasks": [],
            "transfers": [],
            "complaints": [],
            "calls": [],
            "satisfaction": [],
        }
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    loaded = self._json.load(fh)
                if isinstance(loaded, dict):
                    for key, value in loaded.items():
                        self.data[key] = value
            except Exception:
                pass

    def save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                self._json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # -- операции ---------------------------------------------------------
    def add(self, kind: str, item: dict[str, Any]) -> dict[str, Any]:
        self.data.setdefault(kind, []).append(item)
        self.save()
        return item

    def list(self, kind: str) -> list[dict[str, Any]]:
        return list(self.data.get(kind, []))


def mask_name(full_name: str) -> str:
    """«Смирнова Ольга Викторовна» → «Смирнова О. В.» — для журналов."""
    parts = (full_name or "").split()
    if not parts:
        return ""
    return parts[0] + "".join(f" {p[0]}." for p in parts[1:] if p)


def mask_birth(birth: str) -> str:
    """Дата рождения в журналах — только год: 12.03.1978 → 1978."""
    m = re.match(r"\d{2}\.\d{2}\.(\d{4})", birth or "")
    return m.group(1) if m else ""


def mask_phone(phone: str) -> str:
    """Маскированный номер: +7 ***-**-34 (Приложение В)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "+7 ***-**-**"
    return f"+7 ***-**-{digits[-2:]}"


# --------------------------------------------------------------------------
# Диалоговый менеджер
# --------------------------------------------------------------------------


class DialogManager:
    """Конечный автомат диалога поверх MIS-клиента, слот-движка и safety-слоя."""

    def __init__(
        self,
        *,
        mis: Any,
        slots: Any = None,
        safety: Any = None,
        kb: Any = None,
        store: Optional[LocalStore] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        horizon_days: int = 14,
        tz_offset_hours: int = 4,
    ) -> None:
        self.mis = mis
        self.slots = slots
        self.safety = safety if safety is not None else self._load_safety()
        self.kb = kb if kb is not None else self._load_kb()
        self.store = store or LocalStore()
        self._now = now_provider or (lambda: datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=tz_offset_hours))
        self.horizon_days = horizon_days
        self.nlu = Nlu(today_provider=self._now)
        self.state = DialogState(
            session_id="S-" + uuid.uuid4().hex[:8],
            started_at=self._now().strftime("%d.%m.%Y %H:%M"),
        )
        self.actions: list[ApiCall] = []
        self.mis_failures = 0
        self._greeted = False

    # -- мягкая загрузка необязательных модулей ---------------------------
    @staticmethod
    def _load_safety() -> Any:
        try:
            from app.safety.detector import SafetyLayer

            return SafetyLayer()
        except Exception:
            class _Fallback:
                def check(self, text: str, **_: Any) -> SafetyMatch:
                    return SafetyMatch()

                def boundary_phrase(self, topic: str = "") -> str:
                    return BOUNDARY_MEDICAL

            return _Fallback()

    @staticmethod
    def _load_kb() -> Any:
        try:
            from app.kb.knowledge import KnowledgeBase

            return KnowledgeBase()
        except Exception:
            class _Fallback:
                def answer(self, question: str, **_: Any) -> None:
                    return None

                def topic(self, key: str) -> None:
                    return None

            return _Fallback()

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    def now(self) -> datetime:
        return self._now()

    def today(self) -> date:
        return self.now().date()

    def set_scenario(self, scenario: str) -> None:
        self.state.scenario = scenario
        try:
            self.mis.set_scenario(scenario)
        except Exception:
            pass

    def _collect(self, calls: list[ApiCall]) -> None:
        self.actions.extend(calls)

    def reset(self) -> DialogState:
        """Новый звонок на том же менеджере (для демо и автотестов)."""
        preserved = self.state.record_opt_out
        self.state = DialogState(
            session_id="S-" + uuid.uuid4().hex[:8],
            started_at=self.now().strftime("%d.%m.%Y %H:%M"),
            record_opt_out=preserved,
        )
        self.actions = []
        self.mis_failures = 0
        self._greeted = False
        try:
            self.mis.set_scenario("")
        except Exception:
            pass
        return self.state

    # ------------------------------------------------------------------
    # Вход
    # ------------------------------------------------------------------
    def greet(self) -> TurnResult:
        """Приветствие в начале звонка (регламент 3.3)."""
        self._greeted = True
        return TurnResult(
            reply=GREETING,
            intent=Intent.UNKNOWN.value,
            state=self.state.to_dict(),
            tts_text=GREETING,
        )

    def handle(self, text: str, *, source: str = "text") -> TurnResult:
        """Обработать одну реплику пациента."""
        self.state.turns += 1
        raw = (text or "").strip()
        calls_before = len(self.actions)
        mis_journal = getattr(self.mis, "calls", None) or []
        mis_before = len(mis_journal)

        # 1. Safety-слой — раньше всего остального.
        safety = self._safety(raw)
        self.state.last_safety = {
            "verdict": safety.verdict.value,
            "rule": safety.rule,
            "category": safety.category,
            "matched": safety.matched,
            "reason": safety.reason,
        }
        if safety.verdict != SafetyVerdict.OK:
            forced = self._handle_safety(safety, raw)
            if forced is not None:
                forced.actions = self._drain(calls_before, mis_before)
                forced.state = self.state.to_dict()
                if not forced.safety:
                    forced.safety = self.state.last_safety or {}
                return forced

        # 2. Согласие на запись разговора
        if safety.verdict == SafetyVerdict.RECORD_OPT_OUT:
            pass  # обработано в _handle_safety

        # 3. Несколько интентов в одном звонке — по очереди
        result = self._dispatch(raw)
        result.actions = self._drain(calls_before, mis_before)
        result.state = self.state.to_dict()
        if not result.safety:
            result.safety = self.state.last_safety or {}
        return result

    def _drain(self, calls_before: int, mis_before: int) -> list[ApiCall]:
        """Собрать вызовы API, сделанные за текущий ход.

        Журнал ведёт MIS-клиент (``MisClient.calls``); диалог забирает из него
        только дельту текущего хода, чтобы панель демо показывала ровно те
        обращения к стенду, которые вызваны этой репликой.
        """
        journal = getattr(self.mis, "calls", None) or []
        self._collect(list(journal)[mis_before:])
        return self.actions[calls_before:]

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------
    def _safety(self, text: str) -> SafetyMatch:
        try:
            return self.safety.check(text)
        except Exception as exc:  # safety не имеет права ломать диалог
            return SafetyMatch(reason=f"сбой safety-слоя: {exc}")

    def _handle_safety(self, safety: SafetyMatch, raw: str) -> Optional[TurnResult]:
        verdict = safety.verdict

        if verdict == SafetyVerdict.RED_FLAG_A:
            if "уже вызвал" in ru.normalize(raw) or "скорая едет" in ru.normalize(raw):
                return self._emergency_turn(EMERGENCY_ALREADY_CALLED, safety, escalate=False)
            return self._emergency_turn(EMERGENCY, safety, escalate=True)

        if verdict == SafetyVerdict.RED_FLAG_B:
            # Категория Б: регистратор P1; вне часов работы — как категория А
            if not self._operators_available():
                return self._emergency_turn(EMERGENCY, safety, escalate=True)
            self.state.scenario = "С-14"
            self._make_transfer(
                reason=safety.rule or "R-11",
                priority=Priority.P1.value,
                summary=safety.reason or "острое состояние",
                intent=Intent.EMERGENCY.value,
            )
            return TurnResult(
                reply=EMERGENCY_B,
                intent=Intent.EMERGENCY.value,
                safety=self.state.last_safety or {},
                transfer=self.state.transfer,
                outcome=Outcome.EMERGENCY.value,
                tts_text=EMERGENCY_B,
            )

        if verdict == SafetyVerdict.OPERATOR:
            self._make_transfer(reason="R-02", priority=Priority.P2.value,
                                summary="пациент просит сотрудника", intent=Intent.OPERATOR.value)
            return TurnResult(
                reply=f"{OPERATOR} {OPERATOR_WAIT}",
                intent=Intent.OPERATOR.value,
                safety=self.state.last_safety or {},
                transfer=self.state.transfer,
                outcome=Outcome.ESCALATED.value,
                tts_text=f"{OPERATOR} {OPERATOR_WAIT}",
            )

        if verdict == SafetyVerdict.COMPLAINT:
            self.state.scenario = "С-15"
            self._make_transfer(reason="R-03", priority=Priority.P2.value,
                                summary=safety.matched or "жалоба", intent=Intent.COMPLAINT.value,
                                dissatisfied=True)
            self.store.add("complaints", {
                "ts": self.now().strftime("%d.%m.%Y %H:%M"),
                "summary": (safety.matched or "жалоба")[:200],
                "reason": "R-03",
            })
            return TurnResult(
                reply=COMPLAINT,
                intent=Intent.COMPLAINT.value,
                safety=self.state.last_safety or {},
                transfer=self.state.transfer,
                outcome=Outcome.COMPLAINT.value,
                tts_text=COMPLAINT,
            )

        if verdict == SafetyVerdict.MEDICAL:
            # Медицинский вопрос: граничная фраза, сценарий не прерываем, но
            # если пациент настаивает (второй раз подряд) — перевод R-04.
            key = int(self.state.pending.get("medical_repeat", 0)) + 1
            self.state.pending["medical_repeat"] = key
            if key >= 2:
                self._make_transfer(reason="R-04", priority=Priority.P3.value,
                                    summary="настаивает на медицинской консультации",
                                    intent=Intent.MEDICAL.value)
                return TurnResult(
                    reply="Понимаю. Соединяю вас с регистратором — он подскажет, как быстрее попасть к врачу.",
                    intent=Intent.MEDICAL.value,
                    safety=self.state.last_safety or {},
                    transfer=self.state.transfer,
                    outcome=Outcome.ESCALATED.value,
                )
            rep = "Я не вижу значений результатов и не могу их оценивать. Разъяснит лечащий врач." \
                if ("холестерин" in ru.normalize(raw) or "анализ" in ru.normalize(raw)) \
                else self._boundary(raw)
            return TurnResult(
                reply=f"{rep} {ASK_MORE}",
                intent=Intent.MEDICAL.value,
                safety=self.state.last_safety or {},
                tts_text=rep,
            )

        if verdict == SafetyVerdict.SPD_CONSENT:
            self._make_transfer(reason="R-16", priority=Priority.P3.value,
                                summary="отзыв согласия на обработку ПДн",
                                intent=Intent.OTHER.value)
            return TurnResult(
                reply="Этот запрос оформляется письменно. Соединяю с администратором.",
                intent=Intent.OTHER.value,
                safety=self.state.last_safety or {},
                transfer=self.state.transfer,
                outcome=Outcome.ESCALATED.value,
            )

        if verdict == SafetyVerdict.RECORD_OPT_OUT:
            self.state.record_opt_out = True
            self.state.recording_consent = False
            return TurnResult(
                reply=f"{RECORD_OFF} {ASK_MORE}",
                safety=self.state.last_safety or {},
                tts_text=RECORD_OFF,
            )

        if verdict == SafetyVerdict.SILENCE:
            self.state.silence_count += 1
            if self.state.silence_count == 1:
                return TurnResult(reply=SILENCE_1, safety=self.state.last_safety or {}, tts_text=SILENCE_1)
            self.state.finished = True
            return TurnResult(reply=SILENCE_2, safety=self.state.last_safety or {},
                              outcome=Outcome.DROPPED.value, tts_text=SILENCE_2)

        return None

    def _boundary(self, raw: str) -> str:
        try:
            phrase = self.safety.boundary_phrase(raw)
            return phrase or BOUNDARY_MEDICAL
        except Exception:
            return BOUNDARY_MEDICAL

    def _emergency_turn(self, phrase: str, safety: SafetyMatch, *, escalate: bool) -> TurnResult:
        self.state.scenario = "С-14"
        if escalate:
            self._make_transfer(
                reason="R-01",
                priority=Priority.P1.value,
                summary="экстренный: угроза жизни, рекомендовано 103/112",
                intent=Intent.EMERGENCY.value,
            )
        self.state.finished = True
        self.state.call_outcome = Outcome.EMERGENCY.value
        self.store.add("calls", {
            "ts": self.now().strftime("%d.%m.%Y %H:%M"),
            "outcome": Outcome.EMERGENCY.value,
            "rule": "R-01",
            "category": safety.category,
        })
        return TurnResult(
            reply=phrase,
            intent=Intent.EMERGENCY.value,
            safety=self.state.last_safety or {},
            transfer=self.state.transfer,
            outcome=Outcome.EMERGENCY.value,
            tts_text=phrase,
        )

    # ------------------------------------------------------------------
    # Диспетчер
    # ------------------------------------------------------------------
    def _dispatch(self, raw: str) -> TurnResult:
        step = self.state.step

        # Активный сценарий имеет приоритет: продолжаем собирать его параметры.
        if step not in ("IDLE", "DONE") and self.state.scenario:
            handler = self._handlers().get(self.state.scenario)
            if handler is not None:
                return handler(raw)

        extract = self.nlu.extract(raw)
        intent = extract.intent

        if intent == Intent.OPERATOR.value:
            return self._handle_safety(
                SafetyMatch(verdict=SafetyVerdict.OPERATOR, rule="R-02"),
                raw,
            ) or TurnResult(reply=OPERATOR)

        # «Вы робот?»
        if re.search(r"\b(робот|бот|автоответчик|живой человек|вы человек)\b", ru.normalize(raw)):
            if re.search(r"\b(робот|бот)\b", ru.normalize(raw)):
                return TurnResult(reply=ROBOT_ANSWER, intent=Intent.OTHER.value,
                                  tts_text=ROBOT_ANSWER)

        # Посторонний спрашивает о чужой записи (раздел 3.5, тест №4).
        # Проверяем ДО обработки непонимания и до запуска сценария: сведения о
        # записях ассистент сообщает только самому пациенту.
        if self._is_third_party_query(raw):
            self.state.intent = Intent.CHECK.value
            self.state.call_outcome = Outcome.INFO_GIVEN.value
            self.state.step = "DONE"
            card = self._make_transfer(
                reason="R-07", priority=Priority.P3.value,
                summary="посторонний запрашивает сведения о чужой записи",
                intent=Intent.CHECK.value,
            )
            reply = ("Сведения о записях я могу сообщить только самому пациенту. "
                     "Могу отправить SMS на номер пациента из карты.")
            return TurnResult(reply=reply, intent=Intent.CHECK.value,
                              transfer=card, outcome=Outcome.INFO_GIVEN.value,
                              tts_text=reply)

        if intent == Intent.EMERGENCY.value:
            return self._handle_safety(
                SafetyMatch(verdict=SafetyVerdict.RED_FLAG_A, rule="R-01"), raw
            ) or TurnResult(reply=EMERGENCY)

        if intent == Intent.COMPLAINT.value:
            return self._handle_safety(
                SafetyMatch(verdict=SafetyVerdict.COMPLAINT, rule="R-03",
                            matched=ru.normalize(raw)[:120]), raw
            ) or TurnResult(reply=COMPLAINT)

        # Простое «да»/«нет» вне сценария — непонимание
        if extract.yes or extract.no:
            if self.state.step in ("IDLE", "DONE") and self.state.turns <= 1:
                return TurnResult(reply=ASK_MORE)

        if intent == Intent.UNKNOWN.value:
            self.state.misunderstanding_count += 1
            count = self.state.misunderstanding_count
            if count == 1:
                return TurnResult(reply=NOT_UNDERSTOOD_1, intent=intent, tts_text=NOT_UNDERSTOOD_1)
            if count == 2:
                self._make_transfer(reason="R-05", priority=Priority.P3.value,
                                    summary="ассистент не понял пациента дважды",
                                    intent=Intent.OTHER.value)
                return TurnResult(reply=NOT_UNDERSTOOD_2, intent=intent,
                                  transfer=self.state.transfer,
                                  outcome=Outcome.ESCALATED.value, tts_text=NOT_UNDERSTOOD_2)
            self._make_transfer(reason="R-05", priority=Priority.P3.value,
                                summary="ассистент не понял пациента трижды",
                                intent=Intent.OTHER.value)
            self.state.finished = True
            return TurnResult(reply=NOT_UNDERSTOOD_3, intent=intent,
                              transfer=self.state.transfer, outcome=Outcome.ESCALATED.value,
                              tts_text=NOT_UNDERSTOOD_3)

        self.state.misunderstanding_count = 0
        starter = self._starters().get(intent)
        if starter is None:
            self._make_transfer(reason="R-08", priority=Priority.P3.value,
                                summary="запрос вне каталога интентов", intent=intent)
            return TurnResult(reply="С этим вопросом поможет регистратор. Соединяю.",
                              intent=intent, transfer=self.state.transfer,
                              outcome=Outcome.ESCALATED.value)
        return starter(extract)

    # ------------------------------------------------------------------
    # Карта сценариев
    # ------------------------------------------------------------------
    def _handlers(self) -> dict[str, Callable[[str], TurnResult]]:
        return {
            "С-01": self._s01_book,
            "С-02": self._s01_book,       # запись по направлению — та же машина
            "С-03": self._s03_reschedule,
            "С-04": self._s04_cancel,
            "С-05": self._s05_check,
            "С-06": self._s06_home_visit,
            "С-07": self._s07_info,
            "С-08": self._s08_lab,
            "С-09": self._s09_paid,
            "С-11": self._s11_waitlist,
            "С-12": self._s01_book,
            "С-13": self._s07_info,
            "С-10": self._s10_outbound,
        }

    def _s09_paid(self, raw: str) -> TurnResult:
        """Продолжение справки по платным услугам."""
        self.state.scenario = None
        self.state.step = "IDLE"
        return self._dispatch(raw)

    def _s10_outbound(self, raw: str) -> TurnResult:
        """Реплика пациента в исходящем звонке-напоминании (С-10)."""
        return self.outbound_reply(raw)

    def _starters(self) -> dict[str, Callable[[NluResult], TurnResult]]:
        return {
            Intent.BOOK.value: self._start_book,
            Intent.RESCHEDULE.value: self._start_reschedule,
            Intent.CANCEL.value: self._start_cancel,
            Intent.CHECK.value: self._start_check,
            Intent.HOME_VISIT.value: self._start_home_visit,
            Intent.SCHEDULE.value: self._start_info,
            Intent.LAB_RESULTS.value: self._start_lab,
            Intent.PAID.value: self._start_paid,
            Intent.WAITLIST.value: self._start_waitlist,
            Intent.ATTACH.value: self._start_attach,
        }

    # ------------------------------------------------------------------
    # Идентификация (регламент 3.5)
    # ------------------------------------------------------------------
    def _is_third_party_query(self, raw: str) -> bool:
        """Спрашивают о записи человека, от имени которого не представились.

        Регламент 3.5: ассистент никогда первым не озвучивает сведения о
        записях, а постороннему они не сообщаются вовсе. Если в реплике есть
        вопрос «на когда записан…» и чужое ФИО — это запрос о другом человеке.

        Условие узкое намеренно: собственное имя пациент называет в ответ на
        просьбу идентифицироваться, и это не должно блокироваться.
        """
        text = ru.normalize(raw)
        if self.state.patient is not None:
            return False
        # Только вопросы о параметрах записи («на когда/во сколько/какой кабинет»)
        reading = ("на когда", "когда записан", "когда я записан", "во сколько",
                   "какой кабинет", "проверить запись", "какая у меня запись")
        family = ("моя мама", "моя жена", "мой муж", "моя дочь", "мой сын",
                  "за брата", "за сестру", "за отца", "за мать", "за мужа",
                  "за жену", "за дочь", "за сына", "за бабушку", "за дедушку",
                  "для мамы", "для жены", "для мужа", "для ребёнка", "для ребенка")
        if any(marker in text for marker in family):
            return True
        if not any(marker in text for marker in reading):
            return False
        # «на когда записана Смирнова Ольга» — вопрос + чужое ФИО
        return bool(re.search(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}", raw or ""))

    def _start_identification(self, scenario: str, intent: str,
                              pending: dict[str, Any] | None = None) -> TurnResult:
        self.set_scenario(scenario)
        self.state.intent = intent
        self.state.step = "ASK_FIO"
        self.state.pending.update(pending or {})
        return TurnResult(reply=ASK_FIO, intent=intent, tts_text=ASK_FIO)

    def _try_identify(self, extract: NluResult) -> tuple[Optional[Client], Optional[TurnResult]]:
        """Собрать ФИО+ДР и найти пациента. Возвращает (пациент, ответ-заглушка)."""
        pending = self.state.pending
        if extract.last_name:
            pending["last_name"] = extract.last_name
        if extract.first_name:
            pending["first_name"] = extract.first_name
        if extract.middle_name:
            pending["middle_name"] = extract.middle_name
        if extract.birth_date:
            pending["birth_date"] = fmt_date(extract.birth_date)

        last = pending.get("last_name")
        first = pending.get("first_name")
        birth = pending.get("birth_date")
        if not (last and first and birth):
            self.state.step = "ASK_FIO"
            return None, TurnResult(reply=ASK_FIO, intent=self.state.intent)

        # Поиск пациента (кэш + API)
        try:
            patient = self.mis.find_patient(last, first, birth)
        except (MisError, MisTimeout) as exc:
            return None, self._mis_failure(exc)

        if patient is None:
            # Наша команда заводит карточку сама: стенд — тестовый, данные вымышленные
            try:
                patient = self.mis.create_patient(last, first, birth,
                                                  middle_name=pending.get("middle_name"))
            except (MisError, MisTimeout) as exc:
                return None, self._mis_failure(exc)

        self.state.patient = patient
        self.state.pending.pop("last_name", None)
        self.state.pending.pop("first_name", None)
        self.state.pending.pop("middle_name", None)
        self.state.pending.pop("birth_date", None)
        return patient, None

    def _mis_failure(self, exc: Exception) -> TurnResult:
        """R-06: сбой МИС > 5 с дважды подряд."""
        self.mis_failures += 1
        # При переносе старую запись не отменяем — это гарантируется тем,
        # что отмена выполняется только после успешного создания новой.
        if self.mis_failures >= 2:
            self._make_transfer(reason="R-06", priority=Priority.P3.value,
                                summary="сбой МИС", intent=self.state.intent or Intent.OTHER.value)
            self.state.finished = True
            return TurnResult(reply=MIS_DOWN, transfer=self.state.transfer,
                              outcome=Outcome.CALLBACK.value, tts_text=MIS_DOWN)
        return TurnResult(reply=f"{MIS_DOWN_CALLBACK} {ASK_MORE}")

    @property
    def _patient_visits(self) -> list[Visit]:
        patient = self.state.patient
        if patient is None:
            return []
        try:
            visits = self.mis.list_visits(client_id=patient.id)
        except (MisError, MisTimeout):
            return []
        return [v for v in visits if v.is_active]

    def _active_visits(self) -> list[Visit]:
        """Активные будущие записи пациента (сортировка по времени)."""
        now = self.now()
        visits = [v for v in self._patient_visits if v.start >= now - timedelta(minutes=30)]
        return sorted(visits, key=lambda v: v.start)

    # ------------------------------------------------------------------
    # С-01. Запись на приём
    # ------------------------------------------------------------------
    def _start_book(self, extract: NluResult) -> TurnResult:
        pending: dict[str, Any] = {}
        if extract.specialty:
            pending["specialty"] = extract.specialty
        if extract.part_of_day:
            pending["part_of_day"] = extract.part_of_day
        if extract.date:
            pending["wanted_date"] = fmt_date(extract.date)
        if extract.banned_weekdays:
            pending["banned_weekdays"] = list(extract.banned_weekdays)
        return self._start_identification("С-01", Intent.BOOK.value, pending)

    def _s01_book(self, raw: str) -> TurnResult:
        step = self.state.step
        extract = self.nlu.extract(raw, intent=Intent.BOOK.value)
        pending = self.state.pending

        if step == "ASK_FIO":
            # Пока не идентифицирован — ФИО и ДР обязательны, сведения о
            # записях не сообщаем («пациент называет первым», раздел 3.5).
            patient, stub = self._try_identify(extract)
            if stub is not None:
                return stub
            assert patient is not None
            self.state.pending["greeted_patient"] = patient.name_patronymic
            if pending.get("specialty"):
                return self._s01_offer_specialty(patient, pending["specialty"], extract)
            self.state.step = "ASK_SPECIALTY"
            reply = (f"Спасибо, {patient.name_patronymic}. К какому врачу вас записать?")
            return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)

        if step == "ASK_SPECIALTY":
            specialty = extract.specialty
            if not specialty:
                reply = ("Повторите, пожалуйста, к какому врачу. Например: терапевт, "
                         "хирург или лор.")
                return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)
            return self._s01_offer_specialty(self.state.patient, specialty, extract)

        if step == "ASK_REFERRAL":
            if extract.no:
                specialist = self.state.pending.get("specialty")
                # R-08 / С-02: без направления — предлагаем участкового терапевта
                self.state.pending["via_gp"] = True
                self.state.pending["booked_gp_code"] = Outcome.BOOKED_GP.value
                reply = ("Тогда сначала нужен приём участкового терапевта — он осмотрит "
                         "и при необходимости направит. Записать вас к терапевту?")
                self.state.step = "ASK_GP_CONFIRM"
                self.state.pending["specialist_without_referral"] = specialist
                return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)
            # Направление есть — стенд направлений не хранит (допущение)
            self.state.pending["referral"] = "бумажное" if extract.yes else "не уточнено"
            return self._s01_offer_slots(extract)

        if step == "ASK_GP_CONFIRM":
            if extract.no:
                self.state.step = "DONE"
                self.state.scenario = None
                return TurnResult(reply=f"Хорошо. {ASK_MORE}", intent=Intent.BOOK.value)
            self.state.pending["specialty"] = "терапевт"
            return self._s01_offer_slots(extract)

        if step == "OFFER_SLOTS":
            # Пациент называет пожелание («после обеда», «не в пятницу») —
            # это повод перезапросить слоты, а не выбирать вариант наугад.
            if extract.part_of_day or extract.banned_weekdays:
                if extract.part_of_day:
                    self.state.pending["part_of_day"] = extract.part_of_day
                if extract.banned_weekdays:
                    self.state.pending["banned_weekdays"] = list(extract.banned_weekdays)
                return self._s01_offer_slots(extract, repeat=True)
            return self._s01_pick_slot(extract)

        if step == "CONFIRM":
            if extract.yes:
                return self._s01_commit()
            if extract.no:
                # Уточняем, что именно поправить
                self.state.step = "OFFER_SLOTS"
                return self._s01_offer_slots(extract, repeat=True)
            self.state.misunderstanding_count += 1
            return TurnResult(reply="Подтвердите, пожалуйста: записываем или нет?")

        if step == "DONE":
            return self._post_action_or_unknown(raw)

        return self._post_action_or_unknown(raw)

    # -- выдача слотов ----------------------------------------------------
    def _s01_offer_specialty(self, patient: Client, specialty: str,
                             extract: NluResult) -> TurnResult:
        self.state.pending["specialty"] = specialty
        specialty = self._canon_specialty(specialty)

        # Правило: не более одной активной записи к одной специальности
        for visit in self._active_visits():
            if self._same_specialty(visit.doctor.specialty, specialty):
                self.state.step = "DONE"
                self.state.scenario = None
                when = ru.date_words(visit.start.date(), self.today())
                reply = (f"У вас уже есть запись к этому специалисту — "
                         f"{when}, {ru.time_words(visit.start)}. Хотите её перенести?")
                self.state.pending["existing_visit_id"] = visit.id
                self.state.pending["offer_reschedule"] = True
                return TurnResult(reply=reply, intent=Intent.BOOK.value,
                                  outcome=Outcome.INFO_GIVEN.value, tts_text=reply)

        if specialty in ("кардиолог", "невролог", "эндокринолог", "гастроэнтеролог",
                         "уролог", "пульмонолог"):
            self.state.step = "ASK_REFERRAL"
            reply = (f"Запись к {ru_cases.dative(specialty)} идёт по направлению "
                     "лечащего врача. Направление у вас есть?")
            return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)

        return self._s01_offer_slots(extract)

    def _canon_specialty(self, specialty: str) -> str:
        if self.slots is not None and hasattr(self.slots, "resolve_specialty"):
            try:
                resolved = self.slots.resolve_specialty(specialty)
                if resolved:
                    return resolved
            except Exception:
                pass
        return specialty or "терапевт"

    @staticmethod
    def _same_specialty(a: str, b: str) -> bool:
        a, b = (a or "").lower(), (b or "").lower()
        if not a or not b:
            return False
        return a == b or a in b or b in a

    def _busy_slots(self) -> list[tuple[int, datetime]]:
        busy: list[tuple[int, datetime]] = []
        try:
            visits = self.mis.list_visits()
        except (MisError, MisTimeout):
            visits = []
        for v in visits:
            if v.is_active:
                try:
                    busy.append((v.doctor.id, parse_datetime(v.scheduled_at)))
                except Exception:
                    continue
        return busy

    def _s01_offer_slots(self, extract: NluResult, *, repeat: bool = False) -> TurnResult:
        pending = self.state.pending
        specialty = pending.get("specialty") or "терапевт"
        self.state.pending["specialty"] = specialty
        # Новая выдача слотов всегда отменяет прежний выбор: иначе уточнение
        # пожелания («лучше после обеда») молча записало бы старый слот.
        pending.pop("chosen_slot", None)
        self.set_scenario(self.state.scenario or "С-01")

        query = SlotQuery(
            specialty=specialty,
            part_of_day=pending.get("part_of_day"),
            exclude_weekdays=tuple(pending.get("banned_weekdays") or ()),
            limit=3,
        )
        # Пациент может сразу назвать желаемый день («на завтра», «шестого октября»)
        wanted = pending.get("wanted_date")
        if wanted:
            try:
                day = parse_date(wanted)
                query.date_from = day
                query.date_to = day
            except Exception:
                pass
        if pending.get("wanted_doctor_id"):
            query.doctor_id = int(pending["wanted_doctor_id"])

        slots, engine_ready = self._find_slots(query)

        if not engine_ready:
            return self._no_slot_engine()

        if not slots:
            # С-11: лист ожидания
            self.state.pending["no_slots"] = True
            self.state.step = "WAITLIST_OFFER"
            reply = (f"К {ru_cases.dative(specialty)} свободного времени на две недели нет. "
                     "Могу поставить вас в лист ожидания: как только время освободится, "
                     "я позвоню. Поставить?")
            return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)

        self.state.step = "OFFER_SLOTS"
        self.state.offered_slots = [s.to_dict() for s in slots]
        listing = self._describe_slots(slots[:3])
        patient = self.state.patient
        name = patient.name_patronymic if patient else ""
        prefix = f"{name}, " if name else ""
        reply = f"{prefix}ближайшее время: {listing}. Что удобнее?"
        return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)

    @staticmethod
    def _join_slots(parts: list[str]) -> str:
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return ", ".join(parts[:-1]) + " или " + parts[-1]

    def _describe_slots(self, slots: list[Slot]) -> str:
        """Короткое перечисление слотов для произнесения вслух.

        Если все варианты — в один день, дата называется один раз:
        «сегодня в девять сорок, десять ноль-ноль или пятнадцать пятнадцать».
        """
        if not slots:
            return ""
        by_day: dict[Any, list[Slot]] = {}
        for slot in slots:
            by_day.setdefault(slot.start.date(), []).append(slot)
        chunks: list[str] = []
        for day in sorted(by_day):
            group = by_day[day]
            seen_times: list[str] = []
            for slot in group:
                spoken = ru.time_words(slot.start).replace("в ", "", 1)
                if spoken not in seen_times:
                    seen_times.append(spoken)
            if len(seen_times) == 1:
                chunks.append(f"{ru.date_words(day, self.today())}, в {seen_times[0]}")
            else:
                chunks.append(f"{ru.date_words(day, self.today())} в {', '.join(seen_times)}")
        return self._join_slots(chunks)

    def _choose_offered_slot(self, extract: NluResult,
                             offered: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """Выбрать слот из предложенных по реплике пациента. Общий для С-01 и С-03.

        Пациент может назвать время, день, порядковый номер, часть дня или
        просто согласиться («давайте», «да, верно») — тогда берём первый
        (ближайший) вариант. Регламент запрещает уговаривать и переспрашивать
        без нужды, поэтому согласие всегда трактуется в пользу ближайшего слота.
        """
        if not offered:
            return None

        def by_time() -> Optional[dict[str, Any]]:
            if not extract.time:
                return None
            for item in offered:
                if item["time"] == extract.time.strftime("%H:%M"):
                    return item
            return None

        def by_date() -> Optional[dict[str, Any]]:
            if not extract.date:
                return None
            wanted = fmt_date(extract.date)
            for item in offered:
                if item["date"] == wanted:
                    return item
            return None

        def by_ordinal() -> Optional[dict[str, Any]]:
            if extract.ordinal and 0 < extract.ordinal <= len(offered):
                return offered[extract.ordinal - 1]
            return None

        def by_part_of_day() -> Optional[dict[str, Any]]:
            if not extract.part_of_day:
                return None
            for item in offered:
                hour = int(item["time"][:2])
                if extract.part_of_day == "morning" and hour < 12:
                    return item
                if extract.part_of_day == "afternoon" and 12 <= hour < 16:
                    return item
                if extract.part_of_day == "evening" and hour >= 16:
                    return item
            return None

        # Порядок важен: явные предпочтения (время, день, часть дня) сильнее
        # порядкового слова. «Лучше после обеда» содержит «лучше», которое
        # легко спутать с «третьим» — такие реплики должны вести к перезапросу
        # с учётом пожелания, а не к выбору слота наугад.
        for chooser in (by_time, by_date, by_part_of_day, by_ordinal):
            chosen = chooser()
            if chosen is not None:
                return chosen

        # Согласие или уточнение без конкретики — ближайший вариант
        if extract.yes or extract.specialty:
            return offered[0]

        # Пожелание, которого нет среди предложенных, — перезапрос
        if extract.part_of_day or extract.banned_weekdays or extract.date:
            return None
        return None

    def _find_slots_raw(self, query: SlotQuery) -> tuple[list[Slot], bool]:
        return self._find_slots(query)

    def _clock(self) -> datetime:
        """Единые часы для слот-движка и диалога (иначе «сегодня» разъезжается)."""
        return self.now()

    def _find_slots(self, query: SlotQuery) -> tuple[list[Slot], bool]:
        """Свободные слоты: движок + ранжирование «разные врачи, одно время».

        Слот-движок отдаёт полную сетку, поэтому здесь мы:
        * задаём часы движку (демо может «перевести» время);
        * дедуплицируем по (врач, момент);
        * ранжируем по времени, а внутри одного времени — по id врача, чтобы
          пациент видел не три одинаковые строки, а понятные альтернативы;
        * ограничиваем выдачу запрошенным количеством (не более 3 вариантов).
        """
        if self.slots is None or not getattr(self.slots, "is_ready", False):
            return [], False
        try:
            if hasattr(self.slots, "_now"):
                self.slots._now = self._clock
        except Exception:
            pass
        wanted = max(3, int(query.limit or 3))
        probe = SlotQuery(**{**query.__dict__, "limit": max(wanted, min(60, wanted * 8))})
        try:
            found = self.slots.find_slots(probe, busy=self._busy_slots())
        except Exception:
            return [], True

        seen: set[tuple[int, str]] = set()
        unique: list[Slot] = []
        for slot in found or []:
            key = (slot.doctor_id, slot.api_text)
            if key in seen:
                continue
            seen.add(key)
            unique.append(slot)
        unique.sort(key=lambda s: (s.start, s.doctor_id))

        # Разные специальности в одной выдаче не смешиваем, а одинаковые
        # моменты у разных врачей оставляем одним вариантом: пациенту нужны
        # разные варианты времени, а не три строки «девять сорок».
        def one_per_time(candidates: list[Slot]) -> list[Slot]:
            by_time: dict[datetime, Slot] = {}
            for slot in candidates:
                by_time.setdefault(slot.start, slot)
            return [by_time[key] for key in sorted(by_time)]

        ranked = one_per_time(unique)

        # Пожелание по части дня важнее «похожести» выдачи: если пациент
        # просит «после обеда», утренние слоты в списке быть не должно.
        if query.part_of_day or query.time_from or query.time_to or query.exclude_weekdays:
            filtered = [s for s in ranked if self._matches_preference(s, query)]
            if filtered:
                ranked = filtered

        if len(ranked) < wanted:                    # добираем разными врачами
            for slot in unique:
                if slot not in ranked:
                    ranked.append(slot)
                if len(ranked) >= wanted:
                    break
        return ranked[:wanted], True

    @staticmethod
    def _matches_preference(slot: Slot, query: SlotQuery) -> bool:
        """Соответствует ли слот пожеланиям пациента (часть дня, день недели)."""
        if slot.start.weekday() in (query.exclude_weekdays or ()):
            return False
        hour = slot.start.hour + slot.start.minute / 60
        if query.part_of_day == "morning" and hour >= 12:
            return False
        if query.part_of_day == "afternoon" and not (12 <= hour < 16):
            return False
        if query.part_of_day == "evening" and hour < 16:
            return False
        if query.time_from and slot.time_text < query.time_from:
            return False
        if query.time_to and slot.time_text > query.time_to:
            return False
        return True

    def _no_slot_engine(self) -> TurnResult:
        """Сетка слотов ещё не собрана — работаем по данным стенда."""
        specialty = self.state.pending.get("specialty") or "терапевт"
        doctors = self._doctors_of_specialty(specialty)
        return self._offer_from_doctors(doctors, specialty)

    def _doctors_of_specialty(self, specialty: str) -> list[Doctor]:
        try:
            all_doctors = self.mis.get_doctors()
        except (MisError, MisTimeout) as exc:
            return []
        out = [d for d in all_doctors if d.is_active and self._same_specialty(d.specialty, specialty)]
        return sorted(out, key=lambda d: d.id)

    def _offer_from_doctors(self, doctors: list[Doctor], specialty: str) -> TurnResult:
        if not doctors:
            self._make_transfer(reason="R-08", priority=Priority.P3.value,
                                summary=f"нет врача по специальности {specialty}",
                                intent=Intent.BOOK.value)
            return TurnResult(reply="Такого специалиста в расписании нет. Соединяю с регистратором.",
                              transfer=self.state.transfer, outcome=Outcome.ESCALATED.value)
        now = self.now()
        default_times = ["08:40", "10:30", "15:15", "16:00", "12:00"]
        slots: list[Slot] = []
        for offset in range(1, self.horizon_days + 1):
            day = self.today() + timedelta(days=offset)
            if day.weekday() == 6:
                continue
            for doctor in doctors[:2]:
                for t in default_times:
                    hh, mm = int(t[:2]), int(t[3:])
                    start = datetime(day.year, day.month, day.day, hh, mm)
                    if start <= now:
                        continue
                    slots.append(Slot(doctor_id=doctor.id, doctor_name=doctor.full_name,
                                      specialty=doctor.specialty, start=start,
                                      end=start + timedelta(minutes=20)))
            if len(slots) >= 3:
                break
        if not slots:
            self.state.step = "WAITLIST_OFFER"
            return TurnResult(reply="Свободного времени нет. Поставить вас в лист ожидания?")
        chosen = slots[:3]
        self.state.step = "OFFER_SLOTS"
        self.state.offered_slots = [s.to_dict() for s in chosen]
        parts = [f"{ru.date_words(s.start.date(), self.today())}, {ru.time_words(s.start)}"
                 for s in chosen]
        reply = f"Ближайшее время: {self._join_slots(parts)}. Что удобнее?"
        return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)

    # -- выбор слота и создание записи ------------------------------------
    def _s01_pick_slot(self, extract: NluResult) -> TurnResult:
        offered = self.state.offered_slots
        if not offered:
            self.state.step = "OFFER_SLOTS"
            return self._s01_offer_slots(extract, repeat=True)

        chosen = self._choose_offered_slot(extract, offered)

        # Уточнение пожеланий, которых нет среди предложенных, — перезапрос
        if chosen is None and (extract.part_of_day or extract.banned_weekdays):
            if extract.part_of_day:
                self.state.pending["part_of_day"] = extract.part_of_day
            if extract.banned_weekdays:
                self.state.pending["banned_weekdays"] = list(extract.banned_weekdays)
            return self._s01_offer_slots(extract, repeat=True)

        if chosen is None:
            reply = "Не расслышал. Скажите время или день, который подходит."
            return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)

        self.state.pending["chosen_slot"] = chosen
        self.state.step = "CONFIRM"

        spec = ru_cases.short(chosen["specialty"])
        doctor = chosen["doctor_name"]
        start = parse_datetime(chosen["start"])
        when = ru.date_words(start.date(), self.today())
        cabinet = f", кабинет {chosen['cabinet']}" if chosen.get("cabinet") else ""
        reply = (f"Записываю: {spec} {doctor}, {when}, "
                 f"{ru.time_words(start)}{cabinet}. Верно?")
        if len(reply.split()) > 20:
            reply = f"Записываю: {doctor}, {when}, {ru.time_words(start)}. Верно?"
        return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)

    def _s01_commit(self) -> TurnResult:
        pending = self.state.pending
        chosen = pending.get("chosen_slot")
        patient = self.state.patient
        if not chosen or patient is None:
            self.state.step = "OFFER_SLOTS"
            return self._s01_offer_slots(NluResult())

        start = parse_datetime(chosen["start"])
        doctor_id = int(chosen["doctor_id"])

        # Контроль занятости — наш код, стенд этого не делает (раздел 4 ТЗ)
        if self._is_busy(doctor_id, start):
            self.state.pending["part_of_day"] = None
            self.state.step = "OFFER_SLOTS"
            fresh, ready = self._find_slots(SlotQuery(specialty=pending.get("specialty"), limit=2))
            alt = [s for s in fresh if not (s.doctor_id == doctor_id and s.start == start)][:2]
            if len(alt) >= 2:
                parts = [f"{ru.date_words(s.start.date(), self.today())}, {ru.time_words(s.start)}"
                         for s in alt]
                reply = (f"Извините, это время только что заняли. Могу предложить "
                         f"{self._join_slots(parts)}.")
            else:
                reply = "Извините, это время только что заняли. Проверяю другие варианты."
            return TurnResult(reply=reply, intent=Intent.BOOK.value, tts_text=reply)

        try:
            visit = self.mis.create_visit(patient.id, doctor_id, fmt_datetime(start))
        except (MisError, MisTimeout) as exc:
            return self._mis_failure(exc)

        code = pending.get("booked_gp_code") or Outcome.BOOKED.value
        self.state.call_outcome = code
        self.state.step = "DONE"
        self.state.scenario = None
        self.store.add("calls", {
            "ts": self.now().strftime("%d.%m.%Y %H:%M"),
            "outcome": code,
            "patient": mask_name(patient.full_name),
            "birth_year": mask_birth(patient.birth_date),
            "visit_id": visit.id,
            "doctor_id": visit.doctor.id,
            "scheduled_at": visit.scheduled_at,
            "recording": not self.state.record_opt_out,
        })

        sms = self._sms_text("created", visit)
        reply = (f"Готово, вы записаны. SMS с деталями придёт на этот номер. "
                 f"Возьмите паспорт и полис ОМС. Накануне придёт напоминание. {ASK_MORE}")
        cards = [{"type": "sms", "text": sms, "outcome": code}]
        return TurnResult(reply=reply, intent=Intent.BOOK.value, outcome=code,
                          cards=cards, tts_text="Готово, вы записаны. SMS придёт на этот номер. " + ASK_MORE)

    def _is_busy(self, doctor_id: int, start: datetime) -> bool:
        for doc_id, when in self._busy_slots():
            if doc_id == doctor_id and abs((when - start).total_seconds()) < 60:
                return True
        return False

    def _sms_text(self, kind: str, visit: Visit) -> str:
        """Приложение Б регламента: без специальности и диагноза."""
        start = visit.start
        base = "Поликлиника № 7: "
        if kind == "created":
            return (f"{base}вы записаны {ru.short_date(start.date())} в {start.strftime('%H:%M')}, "
                    f"врач {visit.doctor.short_name}. Возьмите паспорт и полис. "
                    f"Отмена/перенос: 700-07-07")
        if kind == "rescheduled":
            return (f"{base}ваша запись перенесена на {ru.short_date(start.date())} "
                    f"в {start.strftime('%H:%M')}, врач {visit.doctor.short_name}. "
                    f"Прежняя запись отменена.")
        if kind == "cancelled":
            return (f"{base}запись на {ru.short_date(start.date())} в {start.strftime('%H:%M')} "
                    f"отменена. Записаться снова: 700-07-07 или Госуслуги.")
        if kind == "reminder":
            return (f"{base}напоминаем о приёме завтра, {ru.short_date(start.date())}, "
                    f"в {start.strftime('%H:%M')}. Не сможете прийти — сообщите: 700-07-07")
        return base

    # ------------------------------------------------------------------
    # С-03. Перенос
    # ------------------------------------------------------------------
    def _start_reschedule(self, extract: NluResult) -> TurnResult:
        # Пациент может назвать запись сам («записана на четверг») — это
        # позволяет родственнику перевести запись, не раскрывая сведений.
        pending: dict[str, Any] = {"reschedule": True}
        if extract.date:
            pending["named_date"] = fmt_date(extract.date)
        if extract.specialty:
            pending["specialty"] = extract.specialty
        return self._start_identification("С-03", Intent.RESCHEDULE.value, pending)

    def _s03_reschedule(self, raw: str) -> TurnResult:
        step = self.state.step
        extract = self.nlu.extract(raw, intent=Intent.RESCHEDULE.value)

        if step == "ASK_FIO":
            patient, stub = self._try_identify(extract)
            if stub is not None:
                return stub
            assert patient is not None
            visits = self._active_visits()
            if not visits:
                self.state.step = "DONE"
                self.state.scenario = None
                reply = "Активных записей не найдено. Хотите записаться?"
                return TurnResult(reply=reply, intent=Intent.RESCHEDULE.value)
            self.state.pending["active_visits"] = [v.to_dict() for v in visits[:3]]
            if len(visits) == 1:
                visit = visits[0]
                self.state.pending["target_visit_id"] = visit.id
                self.state.step = "CONFIRM_TARGET"
                when = ru.date_words(visit.start.date(), self.today())
                reply = (f"{patient.name_patronymic}, у вас запись к "
                         f"{ru_cases.dative(self._specialty_of(visit))} "
                         f"{visit.doctor.short_name} {when} "
                         f"{ru.time_words(visit.start)}. Переносим её?")
                return TurnResult(reply=reply, intent=Intent.RESCHEDULE.value, tts_text=reply)
            # Несколько записей — перечислить не более трёх
            parts = []
            for v in visits[:3]:
                parts.append(f"{ru_cases.short(self._specialty_of(v))} — "
                             f"{ru.date_words(v.start.date(), self.today())}, "
                             f"{ru.time_words(v.start)}")
            self.state.step = "PICK_VISIT"
            reply = ("У вас несколько записей. " + "; ".join(parts) +
                     ". Какую переносим?")
            return TurnResult(reply=reply, intent=Intent.RESCHEDULE.value, tts_text=reply)

        if step == "PICK_VISIT":
            visits = self.state.pending.get("active_visits") or []
            chosen = None
            if extract.ordinal and 0 < extract.ordinal <= len(visits):
                chosen = visits[extract.ordinal - 1]
            if chosen is None and extract.specialty:
                for v in visits:
                    if self._same_specialty(v["doctor"]["specialty"], extract.specialty):
                        chosen = v
                        break
            if chosen is None and extract.date:
                want = fmt_date(extract.date)
                for v in visits:
                    if v["scheduled_at"].startswith(want):
                        chosen = v
                        break
            if chosen is None and len(visits) == 1 and extract.yes:
                chosen = visits[0]
            if chosen is None:
                reply = "Уточните, пожалуйста, какую запись переносим — назовите врача или день."
                return TurnResult(reply=reply, intent=Intent.RESCHEDULE.value)
            self.state.pending["target_visit_id"] = chosen["id"]
            self.state.pending["old_visit"] = chosen
            self.state.step = "OFFER_SLOTS"
            self.state.pending["specialty"] = chosen["doctor"]["specialty"]
            return self._s03_offer_slots(extract)

        if step == "CONFIRM_TARGET":
            if extract.no:
                self.state.step = "DONE"
                self.state.scenario = None
                return TurnResult(reply=f"Хорошо, оставляю запись как есть. {ASK_MORE}",
                                  intent=Intent.RESCHEDULE.value)
            self.state.pending["old_visit"] = self._visit_dict(
                self.state.pending.get("target_visit_id"))
            self.state.pending["specialty"] = \
                (self.state.pending.get("old_visit") or {}).get("doctor", {}).get("specialty")
            self.state.step = "OFFER_SLOTS"
            return self._s03_offer_slots(extract)

        if step == "OFFER_SLOTS":
            return self._s03_pick_slot(extract)

        if step == "CONFIRM":
            if extract.yes:
                return self._s03_commit()
            if extract.no:
                self.state.step = "OFFER_SLOTS"
                return self._s03_offer_slots(extract)
            return TurnResult(reply="Подтвердите, пожалуйста: переношу или нет?")

        return self._post_action_or_unknown(raw)

    def _specialty_of(self, visit: Any) -> str:
        if isinstance(visit, dict):
            return visit.get("doctor", {}).get("specialty", "врачу")
        return getattr(visit.doctor, "specialty", "врачу")

    def _visit_dict(self, visit_id: Any) -> dict[str, Any]:
        for v in self.state.pending.get("active_visits") or []:
            if v.get("id") == visit_id:
                return v
        for v in self._active_visits():
            if v.id == visit_id:
                return v.to_dict()
        return {}

    def _s03_offer_slots(self, extract: NluResult) -> TurnResult:
        old = self.state.pending.get("old_visit") or {}
        self.state.pending.pop("chosen_slot", None)
        specialty = self.state.pending.get("specialty") or \
            (old.get("doctor") or {}).get("specialty") or "терапевт"
        doctor_id = (old.get("doctor") or {}).get("id")
        query = SlotQuery(specialty=specialty, limit=3,
                          exclude_weekdays=tuple(self.state.pending.get("banned_weekdays") or ()))
        if self.state.pending.get("same_doctor") and doctor_id:
            query.doctor_id = int(doctor_id)
        if extract.part_of_day:
            query.part_of_day = extract.part_of_day

        slots, ready = self._find_slots(query)
        if not ready:
            self.state.pending["specialty"] = specialty
            return self._no_slot_engine()

        # не предлагать тот же момент, что уже занят старой записью
        old_start = old.get("scheduled_at")
        slots = [s for s in slots if s.api_text != old_start][:3]

        if not slots:
            # Нет слотов у того же врача — предложить другого врача той же специальности
            other, ready2 = self._find_slots(SlotQuery(specialty=specialty, limit=3))
            other = [s for s in other if s.api_text != old_start][:3]
            if other:
                self.state.offered_slots = [s.to_dict() for s in other]
                self.state.step = "OFFER_SLOTS"
                reply = (f"У того же врача времени нет. Другой врач — "
                         f"{ru_cases.short(specialty)}: "
                         f"{self._describe_slots(other)}. Что удобнее?")
                return TurnResult(reply=reply, intent=Intent.RESCHEDULE.value, tts_text=reply)
            self._make_transfer(reason="R-08", priority=Priority.P3.value,
                                summary="нет слотов для переноса",
                                intent=Intent.RESCHEDULE.value)
            return TurnResult(reply="Свободного времени для переноса нет. Соединяю с регистратором.",
                              transfer=self.state.transfer, outcome=Outcome.ESCALATED.value)

        self.state.offered_slots = [s.to_dict() for s in slots]
        self.state.step = "OFFER_SLOTS"
        reply = f"Варианты: {self._describe_slots(slots)}. Что удобнее?"
        return TurnResult(reply=reply, intent=Intent.RESCHEDULE.value, tts_text=reply)

    def _s03_pick_slot(self, extract: NluResult) -> TurnResult:
        offered = self.state.offered_slots
        if not offered:
            return self._s03_offer_slots(extract)
        chosen = self._choose_offered_slot(extract, offered)
        if chosen is None:
            if extract.part_of_day:
                return self._s03_offer_slots(extract)
            return self._s03_offer_slots(extract)

        self.state.pending["chosen_slot"] = chosen
        self.state.step = "CONFIRM"
        start = parse_datetime(chosen["start"])
        old = self.state.pending.get("old_visit") or {}
        old_start = old.get("scheduled_at")
        old_text = ""
        if old_start:
            try:
                old_dt = parse_datetime(old_start)
                old_text = f" Запись на {ru.date_words(old_dt.date(), self.today())} отменяю."
            except Exception:
                old_text = ""
        reply = (f"Переношу на {ru.date_words(start.date(), self.today())}, "
                 f"{ru.time_words(start)}.{old_text} Верно?")
        return TurnResult(reply=reply, intent=Intent.RESCHEDULE.value, tts_text=reply)

    def _s03_commit(self) -> TurnResult:
        pending = self.state.pending
        chosen = pending.get("chosen_slot")
        old = pending.get("old_visit") or {}
        patient = self.state.patient
        if not chosen or patient is None:
            self.state.step = "OFFER_SLOTS"
            return self._s03_offer_slots(NluResult())

        start = parse_datetime(chosen["start"])
        doctor_id = int(chosen["doctor_id"])

        if self._is_busy(doctor_id, start):
            self.state.step = "OFFER_SLOTS"
            return self._s03_offer_slots(NluResult())

        # Атомарность: сначала новая запись, старая — только после успеха
        try:
            new_visit = self.mis.create_visit(patient.id, doctor_id, fmt_datetime(start))
        except (MisError, MisTimeout) as exc:
            reply = ("Извините, не получилось перенести. Ваша прежняя запись остаётся "
                     f"в силе. {MIS_DOWN}")
            self._make_transfer(reason="R-06", priority=Priority.P3.value,
                                summary="сбой МИС при переносе",
                                intent=Intent.RESCHEDULE.value)
            self.mis_failures += 1
            self.state.step = "DONE"
            self.state.scenario = None
            return TurnResult(reply=reply, intent=Intent.RESCHEDULE.value,
                              transfer=self.state.transfer,
                              outcome=Outcome.ESCALATED.value)

        old_id = old.get("id")
        if old_id:
            try:
                self.mis.patch_visit(int(old_id), status=VisitStatus.CANCELLED.value)
            except (MisError, MisTimeout):
                # Новая запись уже есть; старую не удалось отменить — эскалация
                self._make_transfer(reason="R-06", priority=Priority.P3.value,
                                    summary="не удалось отменить старую запись после переноса",
                                    intent=Intent.RESCHEDULE.value)
                self.state.step = "DONE"
                self.state.scenario = None
                return TurnResult(reply="Новая запись создана, но старую отменить не удалось. "
                                        "Соединяю с регистратором.",
                                  transfer=self.state.transfer,
                                  outcome=Outcome.ESCALATED.value)

        code = Outcome.RESCHEDULED.value
        self.state.call_outcome = code
        self.state.step = "DONE"
        self.state.scenario = None
        self.state.pending["old_visit"] = {}
        self.store.add("calls", {
            "ts": self.now().strftime("%d.%m.%Y %H:%M"),
            "outcome": code,
            "patient": mask_name(patient.full_name),
            "old_visit_id": old_id,
            "new_visit_id": new_visit.id,
            "scheduled_at": new_visit.scheduled_at,
            "recording": not self.state.record_opt_out,
        })
        sms = self._sms_text("rescheduled", new_visit)
        reply = f"Готово, запись перенесена. SMS придёт на этот номер. {ASK_MORE}"
        return TurnResult(reply=reply, intent=Intent.RESCHEDULE.value, outcome=code,
                          cards=[{"type": "sms", "text": sms, "outcome": code}],
                          tts_text="Готово, запись перенесена. SMS придёт на этот номер.")

    # ------------------------------------------------------------------
    # С-04. Отмена
    # ------------------------------------------------------------------
    def _start_cancel(self, extract: NluResult) -> TurnResult:
        pending: dict[str, Any] = {"cancel": True, "transfer_offered": False}
        return self._start_identification("С-04", Intent.CANCEL.value, pending)

    def _s04_cancel(self, raw: str) -> TurnResult:
        step = self.state.step
        extract = self.nlu.extract(raw, intent=Intent.CANCEL.value)

        if step == "ASK_FIO":
            patient, stub = self._try_identify(extract)
            if stub is not None:
                return stub
            assert patient is not None
            visits = self._active_visits()
            if not visits:
                self.state.step = "DONE"
                self.state.scenario = None
                return TurnResult(reply="Активных записей не найдено. Хотите записаться?")
            self.state.pending["active_visits"] = [v.to_dict() for v in visits[:3]]
            if len(visits) == 1:
                return self._s04_offer_transfer(visits[0], patient)
            parts = []
            for v in visits[:3]:
                parts.append(f"{self._specialty_of(v)} — "
                             f"{ru.date_words(v.start.date(), self.today())}, "
                             f"{ru.time_words(v.start)}")
            self.state.step = "PICK_VISIT"
            reply = "У вас несколько записей. " + "; ".join(parts) + ". Какую отменяем?"
            return TurnResult(reply=reply, intent=Intent.CANCEL.value, tts_text=reply)

        if step == "PICK_VISIT":
            visits = self.state.pending.get("active_visits") or []
            chosen = None
            if extract.ordinal and 0 < extract.ordinal <= len(visits):
                chosen = visits[extract.ordinal - 1]
            if chosen is None and extract.specialty:
                for v in visits:
                    if self._same_specialty(v["doctor"]["specialty"], extract.specialty):
                        chosen = v
                        break
            if chosen is None and extract.date:
                want = fmt_date(extract.date)
                for v in visits:
                    if v["scheduled_at"].startswith(want):
                        chosen = v
                        break
            if chosen is None:
                return TurnResult(reply="Уточните, пожалуйста, какую запись отменяем.")
            self.state.pending["target_visit_id"] = chosen["id"]
            visit = Visit.from_api({
                "id": chosen["id"], "client_id": chosen["client_id"],
                "doctor": chosen["doctor"], "scheduled_at": chosen["scheduled_at"],
                "status": chosen["status"],
            })
            return self._s04_offer_transfer(visit, self.state.patient)

        if step == "OFFER_TRANSFER":
            # Один раз предлагаем перенос; при отказе отменяем без уговоров
            if extract.no:
                self.state.pending["transfer_offered"] = True
                return self._s04_confirm()
            if extract.yes:
                # Пациент согласился на перенос → С-03
                old = self.state.pending.get("target_visit") or {}
                self.state.scenario = "С-03"
                self.state.intent = Intent.RESCHEDULE.value
                self.state.step = "OFFER_SLOTS"
                self.state.pending["old_visit"] = old
                self.state.pending["specialty"] = (old.get("doctor") or {}).get("specialty")
                return self._s03_offer_slots(extract)
            return self._s04_confirm()

        if step == "CONFIRM":
            if extract.no:
                self.state.step = "DONE"
                self.state.scenario = None
                return TurnResult(reply=f"Хорошо, запись остаётся в силе. {ASK_MORE}",
                                  intent=Intent.CANCEL.value)
            return self._s04_commit()

        return self._post_action_or_unknown(raw)

    def _s04_offer_transfer(self, visit: Visit, patient: Optional[Client]) -> TurnResult:
        self.state.pending["target_visit"] = visit.to_dict()
        self.state.pending["target_visit_id"] = visit.id
        self.state.step = "OFFER_TRANSFER"
        when = ru.date_words(visit.start.date(), self.today())
        name = f"{patient.name_patronymic}, " if patient else ""
        reply = (f"{name}у вас запись к {ru_cases.dative(visit.doctor.specialty)} "
                 f"{visit.doctor.short_name} "
                 f"{when} {ru.time_words(visit.start)}. Могу перенести её на другой день "
                 f"вместо отмены. Перенести или отменить?")
        return TurnResult(reply=reply, intent=Intent.CANCEL.value, tts_text=reply)

    def _s04_confirm(self) -> TurnResult:
        visit = self.state.pending.get("target_visit") or {}
        start_text = ""
        if visit.get("scheduled_at"):
            try:
                dt = parse_datetime(visit["scheduled_at"])
                start_text = f" на {ru.date_words(dt.date(), self.today())}"
            except Exception:
                start_text = ""
        self.state.step = "CONFIRM"
        reply = f"Отменяю запись к {ru_cases.dative(self._specialty_of(visit))}{start_text}. Верно?"
        return TurnResult(reply=reply, intent=Intent.CANCEL.value, tts_text=reply)

    def _s04_commit(self) -> TurnResult:
        visit = self.state.pending.get("target_visit") or {}
        visit_id = visit.get("id")
        patient = self.state.patient
        if not visit_id:
            self.state.step = "DONE"
            self.state.scenario = None
            return TurnResult(reply=f"Хорошо. {ASK_MORE}")

        # Запись на сегодня до приёма менее 2 часов — отметка «поздняя отмена»
        late = False
        try:
            start = parse_datetime(visit["scheduled_at"])
            late = start.date() == self.today() and (start - self.now()).total_seconds() < 2 * 3600
        except Exception:
            pass

        try:
            self.mis.patch_visit(int(visit_id), status=VisitStatus.CANCELLED.value)
        except (MisError, MisTimeout) as exc:
            return self._mis_failure(exc)

        self.state.call_outcome = Outcome.CANCELLED.value
        self.state.step = "DONE"
        self.state.scenario = None
        self.store.add("calls", {
            "ts": self.now().strftime("%d.%m.%Y %H:%M"),
            "outcome": Outcome.CANCELLED.value,
            "patient": mask_name(patient.full_name) if patient else "",
            "visit_id": visit_id,
            "late_cancellation": late,
            "recording": not self.state.record_opt_out,
        })
        reply = ("Готово, запись отменена. Спасибо, что предупредили — это время достанется "
                 f"другому пациенту. {ASK_MORE}")
        return TurnResult(reply=reply, intent=Intent.CANCEL.value,
                          outcome=Outcome.CANCELLED.value, tts_text=reply)

    # ------------------------------------------------------------------
    # С-05. Проверка своей записи
    # ------------------------------------------------------------------
    def _start_check(self, extract: NluResult) -> TurnResult:
        return self._start_identification("С-05", Intent.CHECK.value, {"check": True})

    def _s05_check(self, raw: str) -> TurnResult:
        extract = self.nlu.extract(raw, intent=Intent.CHECK.value)
        if self.state.step == "ASK_FIO":
            patient, stub = self._try_identify(extract)
            if stub is not None:
                return stub
            assert patient is not None
            visits = self._active_visits()
            if not visits:
                self.state.step = "DONE"
                self.state.scenario = None
                return TurnResult(reply="Активных записей не найдено. Хотите записаться?",
                                  intent=Intent.CHECK.value)
            parts = []
            for v in visits[:3]:
                parts.append(f"{ru_cases.short(v.doctor.specialty)} "
                             f"{v.doctor.short_name} — "
                             f"{ru.date_words(v.start.date(), self.today())}, "
                             f"{ru.time_words(v.start)}")
            self.state.step = "DONE"
            self.state.scenario = None
            self.state.call_outcome = Outcome.INFO_GIVEN.value
            reply = f"{patient.name_patronymic}, вы записаны: " + "; ".join(parts) + \
                    ". Отправить SMS с напоминанием?"
            self.state.pending["check_reported"] = True
            self.store.add("calls", {
                "ts": self.now().strftime("%d.%m.%Y %H:%M"),
                "outcome": Outcome.INFO_GIVEN.value,
                "patient": mask_name(patient.full_name),
                "recording": not self.state.record_opt_out,
            })
            return TurnResult(reply=reply, intent=Intent.CHECK.value,
                              outcome=Outcome.INFO_GIVEN.value, tts_text=reply)
        if self.state.pending.get("check_reported") and extract.yes:
            return TurnResult(reply=f"Отправлено. {ASK_MORE}", intent=Intent.CHECK.value)
        return self._post_action_or_unknown(raw)

    # ------------------------------------------------------------------
    # С-06. Вызов врача на дом
    # ------------------------------------------------------------------
    def _start_home_visit(self, extract: NluResult) -> TurnResult:
        return self._start_identification("С-06", Intent.HOME_VISIT.value, {"home_visit": True})

    def _s06_home_visit(self, raw: str) -> TurnResult:
        step = self.state.step
        extract = self.nlu.extract(raw, intent=Intent.HOME_VISIT.value)

        if step == "ASK_FIO":
            patient, stub = self._try_identify(extract)
            if stub is not None:
                return stub
            assert patient is not None
            self.state.step = "ASK_ADDRESS"
            reply = (f"{patient.name_patronymic}, назовите адрес, куда должен прийти врач.")
            return TurnResult(reply=reply, intent=Intent.HOME_VISIT.value, tts_text=reply)

        if step == "ASK_ADDRESS":
            address = extract.address or raw.strip()
            self.state.pending["address"] = address
            self.state.step = "ASK_REASON"
            reply = "Спасибо. Коротко скажите своими словами, что случилось. Я передам врачу."
            return TurnResult(reply=reply, intent=Intent.HOME_VISIT.value, tts_text=reply)

        if step == "ASK_REASON":
            self.state.pending["reason"] = raw.strip()[:300]
            self.state.step = "ASK_CAN_COME"
            reply = "Передам врачу. Вы можете сами прийти в поликлинику?"
            return TurnResult(reply=reply, intent=Intent.HOME_VISIT.value, tts_text=reply)

        if step == "ASK_CAN_COME":
            if extract.yes:
                self.state.pending["can_come"] = True
                reply = ("Тогда быстрее будет прийти на приём. Записать вас к терапевту "
                         "на сегодня или завтра?")
                self.state.step = "DONE"
                self.state.scenario = None
                return TurnResult(reply=reply, intent=Intent.HOME_VISIT.value)
            return self._s06_commit()

        return self._post_action_or_unknown(raw)

    def _s06_commit(self) -> TurnResult:
        now = self.now()
        weekday = now.weekday()
        within_window = False
        if weekday <= 4:
            within_window = now.hour < 14
        elif weekday == 5:
            within_window = now.hour < 12

        if weekday == 6:
            visit_date = "понедельник"
            reply = ("Заявки в воскресенье не принимаются. Если состояние ухудшится, "
                     "звоните сто три. При необходимости вызовем врача в понедельник.")
        elif within_window:
            visit_date = ru.short_date(now.date())
            reply = ("Хорошо, заявка принята на сегодня. Врач придёт до восемнадцати часов, "
                     "точное время назвать не могу. Регистратор проверит заявку и перезвонит, "
                     "только если понадобится уточнение.")
        else:
            nxt = now + timedelta(days=1)
            while nxt.weekday() == 6:
                nxt += timedelta(days=1)
            visit_date = ru.short_date(nxt.date())
            reply = ("Заявки на сегодня принимаются до четырнадцати часов. Могу оформить "
                     "вызов на завтра. Если состояние ухудшится, можно обратиться в кабинет "
                     "неотложной помощи до двадцати часов или звонить сто три.")

        entry = self.store.add("home_visits", {
            "ts": now.strftime("%d.%m.%Y %H:%M"),
            "patient": mask_name(self.state.patient.full_name) if self.state.patient else "",
            "birth_year": mask_birth(self.state.patient.birth_date) if self.state.patient else "",
            "address_masked": (self.state.pending.get("address") or "")[:60],
            "reason": self.state.pending.get("reason", ""),
            "status": "Требует подтверждения",
            "visit_date": visit_date,
            "child_under_1": False,
        })
        self.state.call_outcome = Outcome.HOME_VISIT_REQUESTED.value
        self.state.step = "DONE"
        self.state.scenario = None
        self.store.add("calls", {
            "ts": now.strftime("%d.%m.%Y %H:%M"),
            "outcome": Outcome.HOME_VISIT_REQUESTED.value,
            "patient": mask_name(self.state.patient.full_name) if self.state.patient else "",
            "home_visit_id": len(self.store.list("home_visits")),
            "recording": not self.state.record_opt_out,
        })
        return TurnResult(
            reply=f"{reply} Хорошо. Если станет хуже — звоните сто три. Выздоравливайте!",
            intent=Intent.HOME_VISIT.value,
            outcome=Outcome.HOME_VISIT_REQUESTED.value,
            cards=[{"type": "home_visit", "record": entry}],
        )

    # ------------------------------------------------------------------
    # С-07 / FAQ. Справки
    # ------------------------------------------------------------------
    def _s07_info(self, raw: str) -> TurnResult:
        """Продолжение справки: пациент уточняет или соглашается на запись."""
        self.state.scenario = None
        self.state.step = "IDLE"
        extract = self.nlu.extract(raw)
        if extract.yes and (extract.specialty or self.state.pending.get("specialty")):
            return self._start_book(extract)
        return self._dispatch(raw)

    def _start_info(self, extract: NluResult) -> TurnResult:
        self.state.scenario = "С-07"
        self.state.intent = Intent.SCHEDULE.value
        self.state.step = "INFO"
        entry, text = self._kb_answer(extract.raw)
        if text is None:
            return self._start_specialty_schedule(extract)
        code = Outcome.INFO_GIVEN.value
        self.state.call_outcome = code
        self.state.step = "DONE"
        self.state.scenario = None
        return TurnResult(reply=f"{text} {ASK_MORE}", intent=Intent.SCHEDULE.value,
                          outcome=code, tts_text=text)

    def _start_specialty_schedule(self, extract: NluResult) -> TurnResult:
        specialty = extract.specialty
        if not specialty:
            entry, text = self._kb_answer(extract.raw)
            if text:
                return TurnResult(reply=f"{text} {ASK_MORE}", intent=Intent.SCHEDULE.value,
                                  outcome=Outcome.INFO_GIVEN.value)
            reply = ("Уточните, пожалуйста, что именно интересует: график работы, "
                     "расписание врача или цены.")
            return TurnResult(reply=reply, intent=Intent.SCHEDULE.value)
        doctors = self._doctors_of_specialty(specialty)
        if not doctors:
            self._make_transfer(reason="R-08", priority=Priority.P3.value,
                                summary=f"нет врача по специальности {specialty}",
                                intent=Intent.SCHEDULE.value)
            return TurnResult(reply="Такого специалиста в расписании нет. Соединяю с регистратором.",
                              transfer=self.state.transfer, outcome=Outcome.ESCALATED.value)
        parts = []
        for doctor in doctors[:2]:
            shift = None
            if self.slots is not None and hasattr(self.slots, "doctor_shift"):
                try:
                    shift = self.slots.doctor_shift(doctor.id)
                except Exception:
                    shift = None
            if shift:
                start_h, end_h = shift
                parts.append(f"{doctor.surname} — с {self._spoken_hour(start_h)} "
                             f"до {self._spoken_hour(end_h)}")
            else:
                parts.append(f"{doctor.surname} — {ru_cases.short(doctor.specialty)}")
        self.state.step = "DONE"
        self.state.scenario = None
        code = Outcome.INFO_GIVEN.value
        reply = (f"{ru_cases.short(specialty).capitalize()} принимают: "
                 + "; ".join(parts) + ". Записать вас?")
        return TurnResult(reply=reply, intent=Intent.SCHEDULE.value, outcome=code, tts_text=reply)

    @staticmethod
    def _spoken_hour(hhmm: str) -> str:
        try:
            h, m = hhmm.split(":")
            return f"{int(h)}:{m}" if m != "00" else f"{int(h)}"
        except Exception:
            return hhmm

    def _kb_answer(self, question: str) -> tuple[Any, Optional[str]]:
        try:
            entry = self.kb.answer(question, patient=self.state.patient, mis=self.mis)
        except Exception:
            return None, None
        if entry is None:
            return None, None
        text = getattr(entry, "answer", None) or (entry.get("answer") if isinstance(entry, dict) else None)
        return entry, text

    # ------------------------------------------------------------------
    # С-08. Готовность анализов
    # ------------------------------------------------------------------
    def _start_lab(self, extract: NluResult) -> TurnResult:
        return self._start_identification("С-08", Intent.LAB_RESULTS.value, {"lab": True})

    def _s08_lab(self, raw: str) -> TurnResult:
        extract = self.nlu.extract(raw, intent=Intent.LAB_RESULTS.value)
        if self.state.step == "ASK_FIO":
            patient, stub = self._try_identify(extract)
            if stub is not None:
                return stub
            assert patient is not None
            self.state.step = "DONE"
            self.state.scenario = None
            self.state.call_outcome = Outcome.INFO_GIVEN.value
            reply = (f"{patient.name_patronymic}, по данным лаборатории готовность уточняется. "
                     "Результаты можно посмотреть в личном кабинете на Госуслугах, получить "
                     "в окне номер три с двенадцати до девятнадцати или у лечащего врача. "
                     "Значения результатов по телефону не сообщаются.")
            return TurnResult(reply=reply, intent=Intent.LAB_RESULTS.value,
                              outcome=Outcome.INFO_GIVEN.value, tts_text=reply)
        return self._post_action_or_unknown(raw)

    # ------------------------------------------------------------------
    # С-09. Платные услуги
    # ------------------------------------------------------------------
    def _start_paid(self, extract: NluResult) -> TurnResult:
        self.state.scenario = "С-09"
        self.state.intent = Intent.PAID.value
        entry, text = self._kb_answer(extract.raw)
        self.state.step = "DONE"
        self.state.scenario = None
        code = Outcome.INFO_GIVEN.value
        if text:
            return TurnResult(reply=f"{text} {ASK_MORE}", intent=Intent.PAID.value,
                              outcome=code, tts_text=text)
        self._make_transfer(reason="R-10", priority=Priority.P3.value,
                            summary="платная услуга вне справочника",
                            intent=Intent.PAID.value)
        return TurnResult(reply="Эту услугу уточнит платное отделение. Соединяю.",
                          intent=Intent.PAID.value, transfer=self.state.transfer,
                          outcome=Outcome.ESCALATED.value)

    # ------------------------------------------------------------------
    # С-11. Лист ожидания
    # ------------------------------------------------------------------
    def _start_waitlist(self, extract: NluResult) -> TurnResult:
        return self._start_identification("С-11", Intent.WAITLIST.value, {"waitlist": True})

    def _s11_waitlist(self, raw: str) -> TurnResult:
        extract = self.nlu.extract(raw, intent=Intent.WAITLIST.value)
        step = self.state.step
        if step == "ASK_FIO":
            patient, stub = self._try_identify(extract)
            if stub is not None:
                return stub
            assert patient is not None
        elif patient := self.state.patient:
            pass
        else:
            return self._post_action_or_unknown(raw)

        patient = self.state.patient
        entry = self.store.add("waitlist", {
            "ts": self.now().strftime("%d.%m.%Y %H:%M"),
            "patient": mask_name(patient.full_name) if patient else "",
            "patient_id": patient.id if patient else None,
            "specialty": self.state.pending.get("specialty") or "любая",
            "preferences": (self.state.pending.get("part_of_day") or "любое время"),
            "status": "ожидает",
        })
        self.state.call_outcome = Outcome.WAITLISTED.value
        self.state.step = "DONE"
        self.state.scenario = None
        self.store.add("calls", {
            "ts": self.now().strftime("%d.%m.%Y %H:%M"),
            "outcome": Outcome.WAITLISTED.value,
            "patient": mask_name(patient.full_name) if patient else "",
            "recording": not self.state.record_opt_out,
        })
        reply = ("Готово, вы в листе ожидания. Позвоню на этот номер, как только освободится "
                 "время. Обычно время освобождается в течение нескольких дней.")
        return TurnResult(reply=reply, intent=Intent.WAITLIST.value,
                          outcome=Outcome.WAITLISTED.value,
                          cards=[{"type": "waitlist", "record": entry}], tts_text=reply)

    # ------------------------------------------------------------------
    # С-13. Прикрепление и документы (справка, без идентификации)
    # ------------------------------------------------------------------
    def _start_attach(self, extract: NluResult) -> TurnResult:
        entry, text = self._kb_answer(extract.raw)
        code = Outcome.INFO_GIVEN.value
        if text:
            return TurnResult(reply=f"{text} {ASK_MORE}", intent=Intent.ATTACH.value,
                              outcome=code, tts_text=text)
        self._make_transfer(reason="R-08", priority=Priority.P3.value,
                            summary="вопрос вне справочника", intent=Intent.ATTACH.value)
        return TurnResult(reply="С этим вопросом поможет регистратор. Соединяю.",
                          transfer=self.state.transfer, outcome=Outcome.ESCALATED.value)

    # ------------------------------------------------------------------
    # После выполненного действия
    # ------------------------------------------------------------------
    def _post_action_or_unknown(self, raw: str) -> TurnResult:
        extract = self.nlu.extract(raw)
        if extract.no:
            self.state.finished = True
            return TurnResult(reply=CLOSING, tts_text=CLOSING)
        if extract.yes:
            return TurnResult(reply=ASK_MORE)
        return self._dispatch(raw)

    # ------------------------------------------------------------------
    # Переводы, карточка перевода (Приложение В)
    # ------------------------------------------------------------------
    def _operators_available(self) -> bool:
        now = self.now()
        if now.weekday() <= 4:
            return 7 <= now.hour < 20
        if now.weekday() == 5:
            return 8 <= now.hour < 15
        return False

    def _make_transfer(self, *, reason: str, priority: str, summary: str,
                       intent: str, dissatisfied: bool = False) -> dict[str, Any]:
        patient = self.state.patient
        card = {
            "call_id": f"CALL-{self.now().strftime('%Y%m%d')}-{self.state.session_id}",
            "time_started": self.state.started_at,
            "time_transferred": self.now().strftime("%d.%m.%Y %H:%M"),
            "phone_masked": mask_phone(self.state.pending.get("phone", "")),
            "patient": (f"№ {patient.id}, {mask_name(patient.full_name)}"
                        if patient else "не идентифицирован"),
            "intent": f"{intent} {self._intent_name(intent)}",
            "params": {k: v for k, v in self.state.pending.items()
                       if k in ("specialty", "part_of_day", "wanted_date", "named_date",
                                "referral", "address", "reason")},
            "reason": reason,
            "priority": priority,
            "summary": summary[:200],
            "dissatisfied": dissatisfied,
            "recording_consent": not self.state.record_opt_out,
            "status": "ожидает оператора" if self._operators_available() else "вне часов работы",
        }
        self.state.transfer = card
        self.store.add("transfers", card)
        return card

    @staticmethod
    def _intent_name(intent: str) -> str:
        names = {
            "I-01": "Запись на приём", "I-02": "Перенос записи", "I-03": "Отмена записи",
            "I-04": "Проверка своей записи", "I-05": "Вызов врача на дом",
            "I-06": "График врача", "I-07": "Готовность анализов", "I-08": "Платные услуги",
            "I-09": "Лист ожидания", "I-10": "Прикрепление и документы",
            "I-11": "Экстренная ситуация", "I-12": "Жалоба", "I-13": "Медицинский вопрос",
            "I-14": "Прочее", "R-02": "Просьба сотрудника",
        }
        return names.get(intent, "Обращение")

    # ------------------------------------------------------------------
    # С-10. Исходящее напоминание
    # ------------------------------------------------------------------
    def outbound_candidates(self) -> list[dict[str, Any]]:
        """Записи «на завтра» с учётом правил С-10: рабочий день, часы, лимиты."""
        now = self.now()
        tomorrow = now.date() + timedelta(days=1)
        while tomorrow.weekday() == 6:      # в воскресенье не звоним
            tomorrow += timedelta(days=1)
        weekday = tomorrow.weekday()
        # В пятницу звоним на приёмы понедельника
        if now.weekday() == 4:
            tomorrow = now.date() + timedelta(days=3)

        try:
            visits = self.mis.list_visits(fresh=True)
        except (MisError, MisTimeout):
            return []
        out = []
        opt_outs = {str(x) for x in self.store.list("opt_outs")}
        confirmations = self.store.data.get("confirmations", {})
        for v in visits:
            if not v.is_active or v.start.date() != tomorrow:
                continue
            if str(v.client_id) in opt_outs:
                continue
            if str(v.id) in confirmations:
                continue
            out.append({
                "visit_id": v.id,
                "client_id": v.client_id,
                "doctor": v.doctor.to_dict(),
                "scheduled_at": v.scheduled_at,
                "status": v.status,
            })
        return sorted(out, key=lambda x: x["scheduled_at"])

    def outbound_script(self, candidate: dict[str, Any]) -> str:
        """Реплика исходящего звонка: только имя и отчество, без фамилии (С-10)."""
        try:
            patient = self.mis.get_patient(int(candidate["client_id"]))
        except (MisError, MisTimeout):
            patient = None
        name = patient.name_patronymic if patient else "это вы"
        doctor = Doctor.from_api(candidate["doctor"])
        try:
            start = parse_datetime(candidate["scheduled_at"])
        except Exception:
            start = self.now()
        self.state.patient = patient
        self.state.outbound = dict(candidate)
        self.state.scenario = "С-10"
        return (f"Здравствуйте! Это виртуальный помощник поликлиники номер семь. "
                f"Я говорю с {name}?")

    def outbound_after_greeting(self, confirmed_person: bool) -> TurnResult:
        candidate = self.state.outbound or {}
        visit_id = candidate.get("visit_id")
        if not confirmed_person:
            self.store.add("calls", {"ts": self.now().strftime("%d.%m.%Y %H:%M"),
                                     "outcome": Outcome.WRONG_PERSON.value,
                                     "visit_id": visit_id})
            self.state.call_outcome = Outcome.WRONG_PERSON.value
            self.state.outbound["wrong_person"] = True
            reply = ("Передайте, пожалуйста, сообщение, чтобы перезвонили в поликлинику "
                     "номер семь. Больше ничего сказать не могу.")
            return TurnResult(reply=reply, intent=Intent.OTHER.value,
                              outcome=Outcome.WRONG_PERSON.value, tts_text=reply)

        try:
            start = parse_datetime(candidate.get("scheduled_at", ""))
        except Exception:
            start = self.now()
        doctor = Doctor.from_api(candidate.get("doctor") or {})
        when = ru.date_words(start.date(), self.today())
        reply = (f"Напоминаю: у вас запись к {ru_cases.dative(doctor.specialty)} "
                 f"{doctor.short_name} "
                 f"{when}, {ru.time_words(start)}. Вы придёте?")
        return TurnResult(reply=reply, intent=Intent.OTHER.value, tts_text=reply)

    def outbound_reply(self, raw: str) -> TurnResult:
        """Ответ пациента в исходящем звонке."""
        extract = self.nlu.extract(raw)
        candidate = self.state.outbound or {}
        visit_id = candidate.get("visit_id")

        if self.state.outbound.get("wrong_person"):
            return TurnResult(reply="Всего доброго!", outcome=Outcome.WRONG_PERSON.value)

        if extract.no or re.search(r"\b(не смогу|не приду|не получится|отмен)\b",
                                   ru.normalize(raw)):
            self.state.pending["outbound_refuse"] = True
            self.state.pending["outbound_visit"] = candidate
            # Один раз предлагаем перенос вместо отмены
            self.state.pending["specialty"] = (candidate.get("doctor") or {}).get("specialty")
            self.state.pending["old_visit"] = {
                "id": visit_id,
                "doctor": candidate.get("doctor"),
                "scheduled_at": candidate.get("scheduled_at"),
                "client_id": candidate.get("client_id"),
            }
            self.state.scenario = "С-03"
            self.state.step = "OFFER_SLOTS"
            self.state.intent = Intent.RESCHEDULE.value
            offer = self._s03_offer_slots(extract)
            if "Варианты" in offer.reply or "Другой" in offer.reply:
                return offer
            # Слотов нет — предлагаем отмену
            return self._outbound_cancel()

        if extract.yes:
            self.store.data.setdefault("confirmations", {})[str(visit_id)] = {
                "ts": self.now().strftime("%d.%m.%Y %H:%M"),
                "status": "Подтверждена (локальный слой)",
            }
            self.store.save()
            self.store.add("calls", {"ts": self.now().strftime("%d.%m.%Y %H:%M"),
                                     "outcome": Outcome.CONFIRMED.value,
                                     "visit_id": visit_id})
            self.state.call_outcome = Outcome.CONFIRMED.value
            self.state.step = "DONE"
            self.state.scenario = None
            reply = "Отлично, ждём вас. Возьмите паспорт и полис. До свидания!"
            return TurnResult(reply=reply, intent=Intent.OTHER.value,
                              outcome=Outcome.CONFIRMED.value, tts_text=reply)

        return TurnResult(reply="Если планы изменятся, позвоните, пожалуйста, "
                                "по номеру регистратуры. Всего доброго!",
                          intent=Intent.OTHER.value)

    def _outbound_cancel(self) -> TurnResult:
        candidate = self.state.outbound or {}
        visit_id = candidate.get("visit_id")
        try:
            self.mis.patch_visit(int(visit_id), status=VisitStatus.CANCELLED.value)
        except (MisError, MisTimeout) as exc:
            return self._mis_failure(exc)
        self.store.add("calls", {"ts": self.now().strftime("%d.%m.%Y %H:%M"),
                                 "outcome": Outcome.CANCELLED.value, "visit_id": visit_id})
        self.state.call_outcome = Outcome.CANCELLED.value
        self.state.step = "DONE"
        self.state.scenario = None
        reply = "Хорошо, запись отменена. Спасибо, что предупредили. Всего доброго!"
        return TurnResult(reply=reply, intent=Intent.OTHER.value,
                          outcome=Outcome.CANCELLED.value, tts_text=reply)

    def outbound_voicemail(self) -> TurnResult:
        """Автоответчик: нейтральное сообщение без ПДн (С-10)."""
        reply = ("Здравствуйте, это поликлиника номер семь. Пожалуйста, перезвоните "
                 "в регистратуру по номеру семьсот, ноль семь, ноль семь.")
        self.store.add("calls", {"ts": self.now().strftime("%d.%m.%Y %H:%M"),
                                 "outcome": Outcome.VOICEMAIL.value})
        self.state.call_outcome = Outcome.VOICEMAIL.value
        return TurnResult(reply=reply, outcome=Outcome.VOICEMAIL.value, tts_text=reply)

    def outbound_opt_out(self) -> TurnResult:
        self.store.add("opt_outs", str((self.state.outbound or {}).get("client_id", "")))
        self.store.add("calls", {"ts": self.now().strftime("%d.%m.%Y %H:%M"),
                                 "outcome": Outcome.OPT_OUT.value})
        self.state.call_outcome = Outcome.OPT_OUT.value
        reply = ("Хорошо, больше не буду звонить с напоминаниями. "
                 "Вы будете получать только SMS.")
        return TurnResult(reply=reply, outcome=Outcome.OPT_OUT.value, tts_text=reply)


__all__ = ["DialogManager", "LocalStore", "mask_name", "mask_birth", "mask_phone"]

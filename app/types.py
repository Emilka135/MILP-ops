"""Голосовой ассистент регистратуры поликлиники № 7 — общий контракт данных.

Прототип кейса № 2 хакатона «Точка сборки».

Этот модуль — ЗАМОРОЖЕННЫЙ интерфейс между подсистемами. Он не выполняет
никакой логики: только перечисления и структуры данных, которыми
обмениваются модули. Меняется только по согласованию со всеми исполнителями.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------
# Форматирование дат стенда (GMT+4, ДД.ММ.ГГГГ и ДД.ММ.ГГГГ ЧЧ:ММ)
# --------------------------------------------------------------------------

DATE_FMT = "%d.%m.%Y"
DATETIME_FMT = "%d.%m.%Y %H:%M"


def fmt_date(d: date) -> str:
    """Дата в формате стенда: 12.09.2026."""
    return d.strftime(DATE_FMT)


def fmt_datetime(dt: datetime) -> str:
    """Момент в формате стенда: 12.09.2026 10:30."""
    return dt.strftime(DATETIME_FMT)


def parse_date(s: str) -> date:
    """Разобрать ДД.ММ.ГГГГ; терпимо к обрамляющим пробелам."""
    return datetime.strptime(s.strip(), DATE_FMT).date()


def parse_datetime(s: str) -> datetime:
    """Разобрать ДД.ММ.ГГГГ ЧЧ:ММ; терпимо к обрамляющим пробелам."""
    return datetime.strptime(s.strip(), DATETIME_FMT)


# --------------------------------------------------------------------------
# Статусы и коды
# --------------------------------------------------------------------------


class VisitStatus(str, Enum):
    """Статусы записи стенда. Других стенд не знает (БП-6)."""

    PLANNED = "Запланирована"
    CANCELLED = "Отменена"
    COMPLETED = "Завершена"


class Outcome(str, Enum):
    """Коды результата звонка (Приложение Г регламента + наш локальный слой)."""

    BOOKED = "BOOKED"
    BOOKED_GP = "BOOKED_GP"
    BOOKED_PAID = "BOOKED_PAID"
    RESCHEDULED = "RESCHEDULED"
    CANCELLED = "CANCELLED"
    CONFIRMED = "CONFIRMED"
    INFO_GIVEN = "INFO_GIVEN"
    WAITLISTED = "WAITLISTED"
    HOME_VISIT_REQUESTED = "HOME_VISIT_REQUESTED"
    ESCALATED = "ESCALATED"
    CALLBACK = "CALLBACK"
    EMERGENCY = "EMERGENCY"
    COMPLAINT = "COMPLAINT"
    GRATITUDE = "GRATITUDE"
    DROPPED = "DROPPED"
    # Исходящие (С-10)
    WRONG_PERSON = "WRONG_PERSON"
    VOICEMAIL = "VOICEMAIL"
    NO_ANSWER = "NO_ANSWER"
    OPT_OUT = "OPT_OUT"


class Priority(str, Enum):
    """Приоритет очереди переводов (раздел 7.5 регламента)."""

    P1 = "P1"  # экстренный: R-01, R-11 — не более 20 секунд
    P2 = "P2"  # высокий: R-02, R-03, R-09, R-15 — не более 30 секунд
    P3 = "P3"  # обычный: остальные — не более 90 секунд


class Intent(str, Enum):
    """Каталог интентов — раздел 4 регламента."""

    BOOK = "I-01"                 # Запись на приём (С-01, С-02)
    RESCHEDULE = "I-02"           # Перенос записи (С-03)
    CANCEL = "I-03"               # Отмена записи (С-04)
    CHECK = "I-04"                # Проверка своей записи (С-05)
    HOME_VISIT = "I-05"           # Вызов врача на дом (С-06)
    SCHEDULE = "I-06"             # График врача / поликлиники (С-07)
    LAB_RESULTS = "I-07"          # Готовность анализов (С-08)
    PAID = "I-08"                 # Платные услуги (С-09)
    WAITLIST = "I-09"             # Лист ожидания (С-11)
    ATTACH = "I-10"               # Прикрепление, полис, документы (С-13, FAQ)
    EMERGENCY = "I-11"            # Экстренная ситуация (С-14)
    COMPLAINT = "I-12"            # Жалоба, конфликт (С-15)
    MEDICAL = "I-13"              # Медицинский вопрос (раздел 3.8)
    OTHER = "I-14"                # Прочее / нестандартное
    OPERATOR = "R-02"             # Просьба соединить с человеком
    UNKNOWN = "UNKNOWN"           # Не распознано; счётчик непониманий


class SafetyVerdict(str, Enum):
    """Решение safety-слоя по реплике пациента."""

    OK = "ok"                       # Обычная реплика
    RED_FLAG_A = "red_flag_a"       # Категория А — 103/112 + P1 (R-01)
    RED_FLAG_B = "red_flag_b"       # Категория Б — регистратор P1 (R-11)
    OPERATOR = "operator"           # «оператор/человек/сотрудник» (R-02)
    COMPLAINT = "complaint"         # Жалоба, агрессия (R-03)
    MEDICAL = "medical"             # Медицинский вопрос → граничная фраза
    SPD_CONSENT = "spd_consent"     # Отзыв согласия на обработку ПДн (R-16)
    RECORD_OPT_OUT = "record_opt_out"  # «не записывайте разговор»
    SILENCE = "silence"             # Тишина/помехи (R-17)
    THIRD_PARTY = "third_party"     # Запрос сведений о другом человеке


@dataclass(frozen=True)
class SafetyMatch:
    """Срабатывание safety-слоя по конкретной реплике."""

    verdict: SafetyVerdict = SafetyVerdict.OK
    rule: str = ""                 # код правила: R-01, R-11, R-02, ...
    category: str = ""             # группа словаря: «Сердце, грудь», «Дыхание», ...
    matched: str = ""              # что именно сработало (фрагмент реплики)
    reason: str = ""               # человекочитаемое объяснение для панели демо


# --------------------------------------------------------------------------
# Справочник врачей, расписание, слоты
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Doctor:
    """Врач справочника стенда (GET /doctors)."""

    id: int
    full_name: str
    specialty: str
    is_active: bool = True

    @property
    def short_name(self) -> str:
        """«Кузнецова А. С.» — как называется врач в SMS и в ответе пациента."""
        parts = self.full_name.split()
        if len(parts) < 2:
            return self.full_name
        initials = " ".join(p[0] + "." for p in parts[1:3])
        return f"{parts[0]} {initials}"

    @property
    def surname(self) -> str:
        return self.full_name.split()[0] if self.full_name else ""

    @property
    def name_patronymic(self) -> str:
        """«Анна Сергеевна» — для обращения и для исходящего звонка С-10."""
        parts = self.full_name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else self.full_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "full_name": self.full_name,
            "specialty": self.specialty,
            "is_active": self.is_active,
        }

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> "Doctor":
        return cls(
            id=int(row["id"]),
            full_name=str(row["full_name"]),
            specialty=str(row["specialty"]),
            is_active=bool(row.get("is_active", True)),
        )


@dataclass(frozen=True)
class DoctorSchedule:
    """Сетка приёма врача, вычисленная из истории записей.

    ``slots_by_weekday`` — обычные времена начала приёма (ЧЧ:ММ) по дням недели
    (0 — понедельник, 6 — воскресенье).
    """

    doctor_id: int
    full_name: str
    specialty: str
    step_minutes: int
    slots_by_weekday: dict[int, tuple[str, ...]] = field(default_factory=dict)

    def slots(self, weekday: int) -> tuple[str, ...]:
        return tuple(self.slots_by_weekday.get(weekday, ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "doctor_id": self.doctor_id,
            "full_name": self.full_name,
            "specialty": self.specialty,
            "step_minutes": self.step_minutes,
            "slots_by_weekday": {str(k): list(v) for k, v in self.slots_by_weekday.items()},
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "DoctorSchedule":
        return cls(
            doctor_id=int(row["doctor_id"]),
            full_name=str(row["full_name"]),
            specialty=str(row["specialty"]),
            step_minutes=int(row["step_minutes"]),
            slots_by_weekday={
                int(k): tuple(v) for k, v in (row.get("slots_by_weekday") or {}).items()
            },
        )


@dataclass(frozen=True)
class Slot:
    """Свободный слот приёма."""

    doctor_id: int
    doctor_name: str
    specialty: str
    start: datetime
    end: datetime
    cabinet: Optional[str] = None

    @property
    def time_text(self) -> str:
        """«08:45» — компактная запись для интерфейса."""
        return self.start.strftime("%H:%M")

    @property
    def api_text(self) -> str:
        """«12.09.2026 10:30» — формат scheduled_at стенда."""
        return fmt_datetime(self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doctor_id": self.doctor_id,
            "doctor_name": self.doctor_name,
            "specialty": self.specialty,
            "start": self.api_text,
            "end": fmt_datetime(self.end),
            "date": fmt_date(self.start),
            "time": self.time_text,
            "weekday": self.start.weekday(),
            "cabinet": self.cabinet,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Slot":
        return cls(
            doctor_id=int(row["doctor_id"]),
            doctor_name=str(row["doctor_name"]),
            specialty=str(row["specialty"]),
            start=parse_datetime(row["start"]),
            end=parse_datetime(row["end"]),
            cabinet=row.get("cabinet"),
        )


@dataclass
class SlotQuery:
    """Запрос свободных слотов. Всё, кроме ``specialty``, необязательно."""

    specialty: Optional[str] = None
    doctor_id: Optional[int] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None          # включительно; по умолчанию горизонт 14 дней
    part_of_day: Optional[str] = None       # morning | afternoon | evening
    time_from: Optional[str] = None         # «13:00»
    time_to: Optional[str] = None           # «18:00»
    weekdays: Optional[tuple[int, ...]] = None   # запрет/разрешение дней — см. SlotFilter
    exclude_weekdays: tuple[int, ...] = ()       # «только не в пятницу»
    limit: int = 3
    include_same_day: bool = True                # «сегодня» — не позже чем за 30 минут


# --------------------------------------------------------------------------
# MIS-клиент, записи, результат вызова API
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Client:
    """Карточка пациента команды в стенде."""

    id: int
    last_name: str
    first_name: str
    middle_name: Optional[str] = None
    birth_date: str = ""           # ДД.ММ.ГГГГ
    comment: Optional[str] = None
    created_at: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.last_name, self.first_name, self.middle_name) if p)

    @property
    def name_patronymic(self) -> str:
        return " ".join(p for p in (self.first_name, self.middle_name) if p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "last_name": self.last_name,
            "first_name": self.first_name,
            "middle_name": self.middle_name,
            "birth_date": self.birth_date,
            "comment": self.comment,
            "created_at": self.created_at,
            "full_name": self.full_name,
        }

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> "Client":
        return cls(
            id=int(row["id"]),
            last_name=str(row.get("last_name") or ""),
            first_name=str(row.get("first_name") or ""),
            middle_name=row.get("middle_name"),
            birth_date=str(row.get("birth_date") or ""),
            comment=row.get("comment"),
            created_at=str(row.get("created_at") or ""),
        )


@dataclass(frozen=True)
class Visit:
    """Запись на приём в стенде."""

    id: int
    client_id: int
    doctor: Doctor
    scheduled_at: str = ""         # как отдал стенд: ДД.ММ.ГГГГ ЧЧ:ММ
    status: str = VisitStatus.PLANNED.value
    created_at: str = ""
    updated_at: str = ""

    @property
    def start(self) -> datetime:
        return parse_datetime(self.scheduled_at)

    @property
    def is_active(self) -> bool:
        return self.status == VisitStatus.PLANNED.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "client_id": self.client_id,
            "doctor": self.doctor.to_dict(),
            "scheduled_at": self.scheduled_at,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> "Visit":
        return cls(
            id=int(row["id"]),
            client_id=int(row["client_id"]),
            doctor=Doctor.from_api(row["doctor"]),
            scheduled_at=str(row.get("scheduled_at") or ""),
            status=str(row.get("status") or VisitStatus.PLANNED.value),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )


@dataclass
class ApiCall:
    """Один вызов API стенда — для журнала демо и разбора сбоев.

    Реплики пациента в журнал не попадают: только метод, путь и код.
    Никаких ПДн (ФИО/ДР) в ``params`` и ``request`` — только идентификаторы.
    """

    ts: str
    method: str
    path: str
    source: str = "MIS"                    # MIS | MOCK | KB
    status: int = 0
    ok: bool = False
    duration_ms: int = 0
    params: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)
    response: Any = None
    error: str = ""
    note: str = ""                          # человекочитаемая подпись для панели
    scenario: str = ""                      # С-01, С-03, ... — к какому сценарию относится

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "method": self.method,
            "path": self.path,
            "source": self.source,
            "status": self.status,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "params": self.params,
            "request": self.request,
            "response": self.response,
            "error": self.error,
            "note": self.note,
            "scenario": self.scenario,
        }


class MisError(Exception):
    """Ошибка MIS-клиента с сохранением ответа стенда."""

    def __init__(
        self,
        message: str,
        status: int = 0,
        code: str = "",
        retry_after: float = 0.0,
        path: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.path = path


class MisTimeout(MisError):
    """Таймаут или недоступность стенда (> 5 с) — основание для правила R-06."""


# --------------------------------------------------------------------------
# База знаний
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KbEntry:
    """Запись справочника ответов (раздел 6 регламента)."""

    topic: str                      # «режим работы», «цены», «анализы»
    route: str                      # раздел/пункт регламента-источника
    answer: str                     # ответ-скрипт для произнесения вслух
    next_action: str = ""           # «предложить запись», «перевод доб. 3»
    follow_up: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "route": self.route,
            "answer": self.answer,
            "next_action": self.next_action,
            "follow_up": self.follow_up,
        }


# --------------------------------------------------------------------------
# Диалог
# --------------------------------------------------------------------------


@dataclass
class DialogState:
    """Состояние конечного автомата диалога. Сериализуется целиком."""

    session_id: str
    scenario: Optional[str] = None          # С-01, С-03, С-04, С-05, С-06, С-10, С-14, С-15
    step: str = "INTRO"
    intent: Optional[str] = None
    patient: Optional[Client] = None
    slots: dict[str, Any] = field(default_factory=dict)
    offered_slots: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] = field(default_factory=dict)
    misunderstanding_count: int = 0
    silence_count: int = 0
    recording_consent: bool = True
    record_opt_out: bool = False
    call_outcome: Optional[str] = None
    transfer: Optional[dict[str, Any]] = None
    last_safety: Optional[dict[str, Any]] = None
    finished: bool = False
    started_at: str = ""
    turns: int = 0
    queue: list[str] = field(default_factory=list)   # несколько интентов в одном звонке
    # Локальный слой (в стенде этого нет — наши допущения)
    server_visits: list[dict[str, Any]] = field(default_factory=list)
    # Служебное для С-10 и тестов
    outbound: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        if isinstance(d.get("patient"), Client):
            d["patient"] = d["patient"].to_dict()
        return d


@dataclass
class TurnResult:
    """Всё, что диалог вернул на одну реплику пациента."""

    reply: str
    intent: str = Intent.UNKNOWN.value
    safety: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    actions: list[ApiCall] = field(default_factory=list)
    transfer: Optional[dict[str, Any]] = None
    cards: list[dict[str, Any]] = field(default_factory=list)
    outcome: Optional[str] = None
    debug: dict[str, Any] = field(default_factory=dict)
    tts_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "intent": self.intent,
            "safety": self.safety,
            "state": self.state,
            "actions": [a.to_dict() for a in self.actions],
            "transfer": self.transfer,
            "cards": self.cards,
            "outcome": self.outcome,
            "debug": self.debug,
        }


__all__ = [
    "DATE_FMT", "DATETIME_FMT",
    "fmt_date", "fmt_datetime", "parse_date", "parse_datetime",
    "VisitStatus", "Outcome", "Priority", "Intent", "SafetyVerdict",
    "SafetyMatch", "Doctor", "DoctorSchedule", "Slot", "SlotQuery",
    "Client", "Visit", "ApiCall", "MisError", "MisTimeout", "KbEntry",
    "DialogState", "TurnResult",
]

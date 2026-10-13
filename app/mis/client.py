"""MIS-клиент стенда «Регистратура» с оффлайн-режимом.

Контракт: ``docs/ARCHITECTURE.md`` §3.1. Спецификация стенда: ``docs/openapi.json``
(источник истины). Модуль использует только стандартную библиотеку
(``urllib.request``), чтобы демо шло без установки зависимостей.

Что закрывает клиент:

* ``Authorization: Bearer <token>`` в каждом запросе;
* собственный ограничитель **10 запросов в минуту** + разбор ``429`` и
  заголовка ``Retry-After`` (ожидание и повтор);
* ответ дольше предела (**5 с**) или обрыв связи → :class:`MisTimeout`
  (основание для правила R-06 «запись не подтверждена»);
* ``400/401/404/409/429/500`` → :class:`MisError` с ``status``, ``code``
  (поле ``code`` из тела) и русским ``message`` стенда;
* кэш справочника врачей в памяти и на диске (``var/doctors_cache.json``);
* пагинация ``limit``/``offset`` (не больше 50 объектов на страницу);
* журнал ``ApiCall`` в ``self.calls`` **без персональных данных**: ФИО и дата
  рождения маскируются (``***``), в журнал идут только идентификаторы;
* автоматический переход в оффлайн-режим (``MockStand``, ``source="MOCK"``),
  если токен пуст, стенд недоступен при старте или отклонил доступ (401).

Токен берётся так: аргумент ``token`` → переменная окружения ``MIS_TOKEN`` →
файл ``var/token.txt``. Базовый адрес — ``MIS_BASE_URL`` (или ``BASE_URL``).
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from collections import deque
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:  # запуск как скрипта из app/web и т. п.
    sys.path.insert(0, str(PROJECT_ROOT))

from app.env import load_env  # noqa: E402  (после правки sys.path)
from app.mis.mock_stand import (  # noqa: E402  (после правки sys.path)
    CODE_BODY,
    CODE_INTERNAL,
    CODE_NOT_FOUND,
    DEFAULT_PATH as MOCK_STAND_PATH,
    MockStand,
)
from app.types import (  # noqa: E402
    ApiCall,
    Client,
    Doctor,
    MisError,
    MisTimeout,
    Visit,
    VisitStatus,
    fmt_date,
    fmt_datetime,
)

# Адрес стенда: он же указан в материалах кейса (блок 6, «Подключение»).
BASE_URL = "https://crm-med-mock.dbi-testing.ru/api/v1"

RATE_LIMIT_REQUESTS = 10          # не больше 10 запросов в минуту на команду
RATE_LIMIT_WINDOW = 60.0
MAX_PAGE = 50                     # предел стенда: 50 объектов на страницу
MAX_TIMEOUT = 5.0                 # предел из §3.1: дольше — MisTimeout (R-06)
MAX_429_RETRIES = 3               # сколько раз повторяем после 429
MAX_RETRY_AFTER = 120.0           # не ждём больше двух минут даже если стенд просит
MAX_PAGINATION_OFFSET = 100_000   # предохранитель от бесконечной постраничности
DEFAULT_TIMEOUT = 5.0

TOKEN_FILE = PROJECT_ROOT / "var" / "token.txt"
CACHE_FILE = PROJECT_ROOT / "var" / "doctors_cache.json"
USER_AGENT = "poliklinika-7-voice-assistant/1.0 (hackathon prototype)"

# Поля персональных данных: в журнал ApiCall они не попадают (152-ФЗ, §4.5).
PII_KEYS = frozenset({
    "last_name", "first_name", "middle_name", "full_name", "name_patronymic",
    "birth_date", "comment", "patient", "phone", "policy", "oms",
})
MASK = "***"

_CODE_BY_STATUS = {
    400: CODE_BODY,
    401: "НЕ_АВТОРИЗОВАН",
    403: "НЕ_АВТОРИЗОВАН",
    404: CODE_NOT_FOUND,
    409: "КОНФЛИКТ_СТАТУСА",
    429: "ПРЕВЫШЕН_ЛИМИТ",
    500: CODE_INTERNAL,
}


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def _sanitize(value: Any) -> Any:
    """Скрыть ПДн в структурах, попадающих в журнал: остаются идентификаторы."""
    if isinstance(value, dict):
        return {
            key: (MASK if key in PII_KEYS else _sanitize(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


def _as_text(value: Any) -> "str | None":
    """Значение фильтра → строка в формате стенда."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return fmt_datetime(value)
    if isinstance(value, date):
        return fmt_date(value)
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def read_token_file(path: "str | Path | None" = None) -> str:
    """Первый непустой некомментированный токен из файла (по умолчанию ``var/token.txt``)."""
    target = TOKEN_FILE if path is None else path
    try:
        text = Path(target).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def resolve_token(explicit: str = "") -> str:
    """Токен: аргумент → ``MIS_TOKEN`` (в т. ч. из ``.env``) → ``var/token.txt``."""
    if explicit and explicit.strip():
        return explicit.strip()
    load_env()
    env = (os.environ.get("MIS_TOKEN") or "").strip()
    if env:
        return env
    return read_token_file()


def mask_token(token: str) -> str:
    """Маскированный токен для логов и панели демо."""
    if not token:
        return ""
    return token[:4] + "…" + token[-2:] if len(token) > 8 else "…"


# --------------------------------------------------------------------------
# Клиент
# --------------------------------------------------------------------------


class MisClient:
    """Клиент стенда «Регистратура» с оффлайн-режимом (контракт ARCHITECTURE.md §3.1)."""

    def __init__(self, *, token: str = "", base_url: str = BASE_URL,
                 store: Any = None, timeout: float = 5.0,
                 calls: "list[ApiCall] | None" = None,
                 mock: bool = False, scenario: str = "") -> None:
        load_env()
        self.base_url = ((base_url or os.environ.get("MIS_BASE_URL") or BASE_URL)).rstrip("/")
        self.timeout = self._coerce_timeout(timeout)
        self._max_timeout = MAX_TIMEOUT
        self.store = store
        self.calls: list[ApiCall] = calls if calls is not None else []
        self._scenario = scenario or ""

        # Настройки, которые можно менять до первого запроса (тесты, демо).
        self.token_path: Path = TOKEN_FILE
        self.cache_path: Path = CACHE_FILE
        self.visits_ttl: float = 20.0              # короткий кэш списка записей
        self.probe_on_start: bool = True           # проверять стенд на старте
        self._mock: "MockStand | None" = None
        self._mock_path: "Path | None" = None      # None → var/mock_stand.json
        self.mock_path = None

        self._token = resolve_token(token)
        self._offline = bool(mock) or not self._token
        self._probed = bool(self._offline)
        if not self._token:
            self.offline_reason = "токен не задан (нет MIS_TOKEN и var/token.txt) — оффлайн-стенд"
        elif mock:
            self.offline_reason = "оффлайн-режим включён принудительно"
        else:
            self.offline_reason = ""

        self._rate: deque = deque()
        self._now: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep

        self._doctors: "list[Doctor] | None" = None
        self.doctors_source: str = ""              # MIS | MOCK | CACHE

        self._visits: "list[Visit] | None" = None
        self._visits_ts: float = 0.0
        self.visits_cache_hits = 0

        # Счётчики для панели демо и отчёта.
        self.requests = 0
        self.retries_429 = 0
        self.rate_waits = 0
        self.fallbacks = 0
        self.last_error = ""
        self._sync_store()

    # -- состояние ---------------------------------------------------------

    @property
    def mock_path(self) -> "Path | None":
        """Файл оффлайн-стенда; ``None`` — ``var/mock_stand.json``.

        Присваивание сбрасывает уже открытый стенд, поэтому сервер демо может
        подменить файл до первого запроса (см. ``app/web/server.py``).
        """
        return self._mock_path

    @mock_path.setter
    def mock_path(self, value: "str | Path | None") -> None:
        self._mock_path = Path(value) if value is not None else None
        self._mock = None

    @property
    def offline(self) -> bool:
        """True — работает оффлайн-стенд (источник MOCK)."""
        return self._offline

    @property
    def token(self) -> str:
        """Токен команды (пусто — оффлайн-режим).

        В журналы и в панель демо выводить нельзя: для показа есть
        :func:`mask_token`, для проверки «задан ли токен» — :attr:`token_set`.
        """
        return self._token

    @property
    def token_set(self) -> bool:
        return bool(self._token)

    @property
    def source(self) -> str:
        return "MOCK" if self._offline else "MIS"

    @property
    def scenario(self) -> str:
        return self._scenario

    def set_scenario(self, scenario: str) -> None:
        """Подпись сценария (С-01, С-03, ...) в последующих ApiCall."""
        self._scenario = str(scenario or "")

    def invalidate_doctors(self, *, disk: bool = False) -> None:
        """Сбросить кэш справочника врачей (память; ``disk=True`` — и файл кэша)."""
        self._doctors = None
        self.doctors_source = ""
        if disk:
            try:
                Path(self.cache_path).unlink()
            except OSError:
                pass

    def invalidate_visits(self) -> None:
        """Сбросить короткий кэш списка записей (после правок стенда вручную)."""
        self._visits = None
        self._visits_ts = 0.0

    def go_offline(self, reason: str = "оффлайн-режим включён вручную") -> None:
        self._go_offline(reason)

    def reload_token(self) -> bool:
        """Перечитать ``MIS_TOKEN``/``var/token.txt``; True — токен появился/сменился."""
        token = resolve_token("") or read_token_file(self.token_path)
        if token and token != self._token:
            self._token = token
            self.offline_reason = ""
            self._probed = False
            self._offline = False
            self.last_error = ""
            self._sync_store()
            return True
        return False

    def health(self) -> dict:
        """Сводка для ``GET /api/health`` панели демо."""
        return {
            "offline": self._offline,
            "source": self.source,
            "token_set": bool(self._token),
            "token_masked": mask_token(self._token),
            "base_url": self.base_url,
            "offline_reason": self.offline_reason,
            "doctors_cached": len(self._doctors or []),
            "doctors_source": self.doctors_source,
            "calls": len(self.calls),
            "requests": self.requests,
            "retries_429": self.retries_429,
            "rate_waits": self.rate_waits,
            "rate_limit_per_min": RATE_LIMIT_REQUESTS,
            "timeout_seconds": self._effective_timeout(),
            "timeout_limit": self._max_timeout,
            "cache_path": str(self.cache_path),
            "last_error": self.last_error,
        }

    # -- справочник врачей -------------------------------------------------

    def get_doctors(self, *, specialty: "str | None" = None,
                    force: bool = False) -> "list[Doctor]":
        """Справочник врачей: память → стенд → кэш на диске → оффлайн-стенд."""
        self._ensure_probe()
        if not force and self._doctors is not None:
            return self._filter_doctors(self._doctors, specialty)

        docs: "list[Doctor]"
        if self._offline:
            docs = self._mock_doctors()
            self.doctors_source = "MOCK"
        else:
            try:
                docs = self._fetch_all_doctors()
                self.doctors_source = "MIS"
                self._save_cache(docs, "MIS")
            except MisError as exc:
                self.last_error = exc.message
                if isinstance(exc, MisTimeout):
                    self._go_offline(f"стенд не ответил: {exc.message}")
                cached = self._load_cache(require_source="MIS")
                if cached:
                    docs = cached
                    self.doctors_source = "CACHE"
                    self.offline_reason = self.offline_reason or f"справочник из кэша: {exc.message}"
                else:
                    self._go_offline(f"справочник недоступен: {exc.message}")
                    docs = self._mock_doctors()
                    self.doctors_source = "MOCK"
        self._doctors = docs
        return self._filter_doctors(docs, specialty)

    def _filter_doctors(self, docs: "Iterable[Doctor]", specialty: "str | None") -> "list[Doctor]":
        rows = sorted(docs, key=lambda d: d.id)
        if specialty and str(specialty).strip():
            needle = " ".join(str(specialty).split()).casefold()
            rows = [d for d in rows if d.specialty.casefold() == needle]
        return list(rows)

    def _fetch_all_doctors(self) -> "list[Doctor]":
        rows: list[dict] = []
        offset = 0
        while True:
            page = self._http_call("GET", "/doctors",
                                   query={"limit": MAX_PAGE, "offset": offset},
                                   note="справочник врачей", fallback_mock=False)
            if not isinstance(page, list):
                raise MisError("Стенд вернул не список врачей.", 500, CODE_INTERNAL, path="/doctors")
            rows.extend(page)
            if len(page) < MAX_PAGE or offset >= MAX_PAGINATION_OFFSET:
                break
            offset += MAX_PAGE
        return [Doctor.from_api(row) for row in rows]

    def _mock_doctors(self) -> "list[Doctor]":
        rows = self._mock_call("GET", "/doctors",
                               {"limit": MAX_PAGE, "offset": 0}, None,
                               "справочник врачей (оффлайн-стенд)")
        return [Doctor.from_api(row) for row in rows]

    # -- пациенты ----------------------------------------------------------

    def find_patient(self, last_name: str, first_name: str,
                     birth_date: str) -> "Client | None":
        """Пациент по ключу стенда: фамилия + имя + дата рождения."""
        found = self.search_clients(last_name=last_name, first_name=first_name,
                                    birth_date=birth_date, limit=1)
        return found[0] if found else None

    def search_clients(self, **filters: Any) -> "list[Client]":
        """Поиск пациентов; поддерживаются last_name, first_name, birth_date, limit, offset."""
        self._ensure_probe()
        allowed = ("last_name", "first_name", "birth_date")
        base = {key: _as_text(value) for key, value in filters.items()
                if key in allowed and value not in (None, "")}
        want = self._int_filter(filters.get("limit"), "limit")
        offset = self._int_filter(filters.get("offset"), "offset")
        found: "list[Client]" = []
        while True:
            query = dict(base, limit=MAX_PAGE, offset=offset)
            page = self._call("GET", "/clients", query=query, note="поиск пациентов")
            found.extend(Client.from_api(row) for row in page)
            if want and len(found) >= want:
                return found[:want]
            if len(page) < MAX_PAGE or offset >= MAX_PAGINATION_OFFSET:
                return found
            offset += MAX_PAGE

    def create_patient(self, last_name: str, first_name: str, birth_date: str,
                       middle_name: "str | None" = None,
                       comment: "str | None" = None) -> Client:
        """Регистрация пациента. Стенд отвечает 201 (создан) или 200 (карточка уже была)."""
        self._ensure_probe()
        self._validate_patient(last_name, first_name, birth_date)
        body: dict[str, Any] = {
            "last_name": last_name, "first_name": first_name, "birth_date": birth_date,
        }
        if middle_name is not None:
            body["middle_name"] = middle_name
        if comment is not None:
            body["comment"] = comment
        row = self._call("POST", "/clients", body=body, note="регистрация пациента")
        return Client.from_api(row)

    def patch_patient(self, client_id: int, **fields: Any) -> Client:
        """Изменить отчество/комментарий. Фамилия, имя и дата рождения — ключ, их менять нельзя."""
        self._ensure_probe()
        bad = sorted(set(fields) - {"middle_name", "comment"})
        if bad:
            raise MisError(
                "Поля " + ", ".join(f"«{name}»" for name in bad)
                + " образуют ключ клиента и изменению не подлежат.",
                400, CODE_BODY, path=f"/clients/{client_id}",
            )
        row = self._call("PATCH", f"/clients/{int(client_id)}", body=dict(fields),
                         note="изменение карточки пациента")
        return Client.from_api(row)

    def get_patient(self, client_id: int) -> Client:
        row = self._call("GET", f"/clients/{int(client_id)}", note="карточка пациента")
        return Client.from_api(row)

    @staticmethod
    def _int_filter(value: Any, name: str) -> int:
        if value in (None, ""):
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            raise MisError(f"Параметр «{name}» должен быть целым числом.", 400, CODE_BODY)

    @staticmethod
    def _validate_patient(last_name: str, first_name: str, birth_date: str) -> None:
        """Локальная проверка обязательных полей — экономит запросы из лимита 10/мин."""
        for field, value in (("last_name", last_name), ("first_name", first_name)):
            if not isinstance(value, str) or not value.strip():
                raise MisError(
                    f"Поле «{field}» обязательно и должно содержать непустое значение строкой.",
                    400, CODE_BODY,
                )
        if not isinstance(birth_date, str) or not birth_date.strip():
            raise MisError("Поле «birth_date» обязательно и должно содержать дату ДД.ММ.ГГГГ.",
                           400, CODE_BODY)
        text = birth_date.strip()
        match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", text)
        if match:
            try:
                datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))
                return
            except ValueError:
                pass
        raise MisError("Поле «birth_date» должно содержать дату в формате ДД.ММ.ГГГГ.",
                       400, CODE_BODY)

    # -- записи на приём ---------------------------------------------------

    def list_visits(self, *, client_id: "int | None" = None,
                    status: "str | None" = None, date_from: "str | None" = None,
                    date_to: "str | None" = None, doctor_id: "int | None" = None,
                    fresh: bool = False) -> "list[Visit]":
        """Записи команды. Без фильтров — из короткого кэша, с фильтрами — всегда со стенда."""
        self._ensure_probe()
        prepared = {
            "client_id": None if client_id in (None, "") else int(client_id),
            "doctor_id": None if doctor_id in (None, "") else int(doctor_id),
            "status": _as_text(status),
            "date_from": _as_text(date_from),
            "date_to": _as_text(date_to),
        }
        has_filter = any(value not in (None, "") for value in prepared.values())
        if not fresh and not has_filter:
            return list(self._visits_all())
        rows: "list[Visit]" = []
        offset = 0
        while True:
            query = {key: value for key, value in prepared.items() if value not in (None, "")}
            query.update(limit=MAX_PAGE, offset=offset)
            page = self._call("GET", "/visits", query=query, note="записи пациента")
            rows.extend(Visit.from_api(row) for row in page)
            if len(page) < MAX_PAGE or offset >= MAX_PAGINATION_OFFSET:
                return rows
            offset += MAX_PAGE

    def _visits_all(self) -> "list[Visit]":
        now = self._now()
        if self._visits is not None and (now - self._visits_ts) <= self.visits_ttl:
            self.visits_cache_hits += 1
            return self._visits
        rows: "list[Visit]" = []
        offset = 0
        while True:
            page = self._call("GET", "/visits", query={"limit": MAX_PAGE, "offset": offset},
                              note="все записи команды")
            rows.extend(Visit.from_api(row) for row in page)
            if len(page) < MAX_PAGE or offset >= MAX_PAGINATION_OFFSET:
                break
            offset += MAX_PAGE
        self._visits = rows
        self._visits_ts = self._now()
        return rows

    def get_visit(self, visit_id: int) -> Visit:
        row = self._call("GET", f"/visits/{int(visit_id)}", note="карточка записи")
        return Visit.from_api(row)

    def create_visit(self, client_id: int, doctor_id: int, scheduled_at: str) -> Visit:
        self._ensure_probe()
        body = {"client_id": int(client_id), "doctor_id": int(doctor_id),
                "scheduled_at": _as_text(scheduled_at)}
        row = self._call("POST", "/visits", body=body, note="создание записи")
        self._visits = None
        return Visit.from_api(row)

    def patch_visit(self, visit_id: int, *, doctor_id: "int | None" = None,
                    scheduled_at: "str | None" = None,
                    status: "str | None" = None) -> Visit:
        """Перенос, смена врача, отмена и завершение записи."""
        self._ensure_probe()
        body: dict[str, Any] = {}
        if doctor_id is not None:
            body["doctor_id"] = int(doctor_id)
        if scheduled_at is not None:
            body["scheduled_at"] = _as_text(scheduled_at)
        if status is not None:
            body["status"] = _as_text(status)
        if not body:
            return self.get_visit(visit_id)
        row = self._call("PATCH", f"/visits/{int(visit_id)}", body=body, note="изменение записи")
        self._visits = None
        return Visit.from_api(row)

    def cancel_visit(self, visit_id: int) -> Visit:
        """Отмена записи (запись сохраняется в стенде — удаления API не даёт)."""
        return self.patch_visit(visit_id, status=VisitStatus.CANCELLED.value)

    def reschedule_visit(self, visit_id: int, *, doctor_id: "int | None" = None,
                         scheduled_at: "str | None" = None) -> Visit:
        """Перенос по правилу R-06: сначала новая запись, потом отмена старой.

        Если создать новую запись не удалось — старая остаётся в силе (исключение
        уходит наверх). Возвращает новую запись.
        """
        old = self.get_visit(visit_id)
        if not old.is_active:
            raise MisError(
                f"Запись в статусе «{old.status}» переносу не подлежит.",
                409, "КОНФЛИКТ_СТАТУСА", path=f"/visits/{visit_id}",
            )
        new_visit = self.create_visit(
            old.client_id,
            int(doctor_id) if doctor_id is not None else old.doctor.id,
            _as_text(scheduled_at) or old.scheduled_at,
        )
        self.cancel_visit(visit_id)
        return new_visit

    # -- журнал и переход в оффлайн ---------------------------------------

    def _log(self, *, method: str, path: str, source: str, status: int, ok: bool,
             duration_ms: int, params: Any = None, request: Any = None,
             response: Any = None, error: str = "", note: str = "") -> ApiCall:
        call = ApiCall(
            ts=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            method=method, path=path, source=source, status=status, ok=ok,
            duration_ms=int(duration_ms),
            params=_sanitize(params or {}),
            request=_sanitize(request or {}),
            response=_sanitize(response),
            error=error, note=note, scenario=self._scenario,
        )
        self.calls.append(call)
        store = self.store
        if store is not None and hasattr(store, "record_call"):
            try:
                store.record_call(call)
            except Exception:  # журнал не должен ломать диалог
                pass
        self._sync_store()
        return call

    def _sync_store(self) -> None:
        """Если владелец передал dict — держим в нём сводку состояния MIS."""
        if isinstance(self.store, dict):
            self.store["mis"] = {
                "offline": self._offline, "source": self.source,
                "token_set": bool(self._token), "doctors": len(self._doctors or []),
                "reason": self.offline_reason, "calls": len(self.calls),
            }

    def _go_offline(self, reason: str) -> None:
        if not self._offline:
            self.fallbacks += 1
        self._offline = True
        self._probed = True
        if reason:
            self.offline_reason = reason
            self.last_error = reason
        self._sync_store()

    def _ensure_probe(self) -> None:
        """Один раз проверить доступ к стенду; при неудаче уйти в оффлайн-режим."""
        if self._probed or self._offline:
            return
        self._probed = True
        if not self.probe_on_start:
            return
        try:
            self._http_call("GET", "/doctors", query={"limit": 1},
                            note="проверка доступа к стенду", fallback_mock=False)
        except MisError as exc:
            self.last_error = exc.message
            self._go_offline(f"стенд недоступен: {exc.message}")

    # -- транспорт ---------------------------------------------------------

    def _call(self, method: str, path: str, *, query: Any = None, body: Any = None,
              note: str = "", fallback_mock: bool = True) -> Any:
        """Один логический вызов API: либо стенд, либо оффлайн-стенд."""
        if self._offline:
            return self._mock_call(method, path, query, body, note)
        try:
            return self._http_call(method, path, query=query, body=body, note=note,
                                   fallback_mock=fallback_mock)
        except MisTimeout as exc:
            # Стенд не ответил: ошибку отдаём наверх (R-06 — «запись не подтверждена»),
            # но дальше работаем оффлайн, чтобы демо не падало.
            self._go_offline(f"стенд не ответил: {exc.message}")
            raise

    def _http_call(self, method: str, path: str, *, query: Any = None, body: Any = None,
                   note: str = "", fallback_mock: bool = True) -> Any:
        self.requests += 1
        url = self._build_url(path, query)
        payload = (json.dumps(body, ensure_ascii=False).encode("utf-8")
                   if body is not None else None)
        attempt = 0
        while True:
            self._throttle()
            started = time.perf_counter()
            try:
                status, headers, raw = self._send(method, url, payload,
                                                  self._effective_timeout(), path)
            except MisTimeout as exc:
                ms = int((time.perf_counter() - started) * 1000)
                self.last_error = exc.message
                self._log(method=method, path=path, source="MIS", status=0, ok=False,
                          duration_ms=ms, params=query, request=body,
                          error=exc.message, note=note)
                raise
            ms = int((time.perf_counter() - started) * 1000)

            if ms / 1000.0 > self._max_timeout:
                message = (f"Ответ стенда занял {ms / 1000:.2f} с — больше предела "
                           f"{self._max_timeout:.0f} с.")
                self.last_error = message
                self._log(method=method, path=path, source="MIS", status=0, ok=False,
                          duration_ms=ms, params=query, request=body, error=message, note=note)
                raise MisTimeout(message, 0, "ТАЙМАУТ", path=path)

            if status == 429 and attempt < MAX_429_RETRIES:
                retry_after = self._retry_after(headers)
                attempt += 1
                self.retries_429 += 1
                self._log(method=method, path=path, source="MIS", status=429, ok=False,
                          duration_ms=ms, params=query, request=body,
                          error=f"ПРЕВЫШЕН_ЛИМИТ, повтор через {retry_after:.0f} с",
                          note=(note + " — лимит частоты").strip(" —"))
                self._sleep(retry_after)
                continue

            if status >= 400:
                code, message = self._parse_error(raw, status)
                self.last_error = message
                self._log(method=method, path=path, source="MIS", status=status, ok=False,
                          duration_ms=ms, params=query, request=body, error=message, note=note)
                if status in (401, 403):
                    self._go_offline(f"стенд отклонил доступ ({status}): {message}")
                    if fallback_mock:
                        return self._mock_call(method, path, query, body,
                                               (note + " → оффлайн-стенд").strip(" →"))
                raise MisError(message, status, code,
                               retry_after=self._retry_after(headers) if status == 429 else 0.0,
                               path=path)

            try:
                data = self._parse_json(raw, status, path)
            except MisError as exc:
                self.last_error = exc.message
                self._log(method=method, path=path, source="MIS", status=status, ok=False,
                          duration_ms=ms, params=query, request=body,
                          error=exc.message, note=note)
                raise
            self._log(method=method, path=path, source="MIS", status=status, ok=True,
                      duration_ms=ms, params=query, request=body, response=data, note=note)
            return data

    def _send(self, method: str, url: str, payload: "bytes | None",
              timeout: float, path: str) -> "tuple[int, dict, bytes]":
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urlrequest.Request(url, data=payload, headers=headers, method=method.upper())
        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                return (int(resp.status),
                        {str(k).lower(): str(v) for k, v in resp.headers.items()},
                        resp.read())
        except urlerror.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            return (int(exc.code),
                    {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()},
                    raw or b"")
        except (socket.timeout, TimeoutError):
            raise MisTimeout(f"Стенд не ответил за {timeout:.1f} с по пути «{path}».",
                             0, "ТАЙМАУТ", path=path)
        except urlerror.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise MisTimeout(f"Стенд не ответил за {timeout:.1f} с по пути «{path}».",
                                 0, "ТАЙМАУТ", path=path)
            raise MisTimeout(f"Стенд недоступен по пути «{path}»: {reason}.",
                             0, "СТЕНД_НЕДОСТУПЕН", path=path)

    def _mock_call(self, method: str, path: str, query: Any, body: Any, note: str) -> Any:
        stand = self.mock_stand
        started = time.perf_counter()
        try:
            status, payload = stand.handle(method, path, query, body)
        except MisError as exc:
            ms = int((time.perf_counter() - started) * 1000)
            self.last_error = exc.message
            self._log(method=method, path=path, source="MOCK", status=exc.status or 400,
                      ok=False, duration_ms=ms, params=query, request=body,
                      error=exc.message, note=note)
            raise
        ms = int((time.perf_counter() - started) * 1000)
        self._log(method=method, path=path, source="MOCK", status=status, ok=True,
                  duration_ms=ms, params=query, request=body, response=payload, note=note)
        return payload

    @property
    def mock_stand(self) -> MockStand:
        if self._mock is None:
            self._mock = MockStand(self._mock_path if self._mock_path is not None
                                   else MOCK_STAND_PATH)
        return self._mock

    # -- ограничитель частоты ---------------------------------------------

    def _throttle(self) -> None:
        """Не больше 10 запросов в минуту: при исчерпании окна — ждём."""
        while True:
            now = self._now()
            while self._rate and now - self._rate[0] >= RATE_LIMIT_WINDOW:
                self._rate.popleft()
            if len(self._rate) < RATE_LIMIT_REQUESTS:
                self._rate.append(now)
                return
            wait = RATE_LIMIT_WINDOW - (now - self._rate[0]) + 0.05
            if wait <= 0:
                continue
            self.rate_waits += 1
            self._sleep(wait)

    @staticmethod
    def _retry_after(headers: dict) -> float:
        """``Retry-After`` в секундах (стенд отдаёт целое)."""
        raw = headers.get("retry-after", "")
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            value = RATE_LIMIT_WINDOW
        if value <= 0:
            value = 1.0
        return max(1.0, min(value, MAX_RETRY_AFTER))

    def _effective_timeout(self) -> float:
        return max(0.05, min(self.timeout, self._max_timeout))

    @staticmethod
    def _coerce_timeout(value: Any) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT
        return timeout if timeout > 0 else DEFAULT_TIMEOUT

    # -- разбор ответов ----------------------------------------------------

    def _build_url(self, path: str, query: Any) -> str:
        url = f"{self.base_url}{path}"
        if query:
            pairs = [(key, value) for key, value in dict(query).items()
                     if value is not None and value != ""]
            if pairs:
                url += "?" + urlparse.urlencode(pairs)
        return url

    @staticmethod
    def _parse_json(raw: bytes, status: int, path: str) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise MisError(f"Стенд вернул не JSON (код {status}) по пути «{path}».",
                           status, CODE_INTERNAL, path=path)

    @staticmethod
    def _parse_error(raw: bytes, status: int) -> "tuple[str, str]":
        code, message = "", ""
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(body, dict):
                code = str(body.get("code") or "")
                message = str(body.get("message") or "")
        except ValueError:
            pass
        if not code:
            code = _CODE_BY_STATUS.get(status, CODE_INTERNAL)
        if not message:
            message = f"Стенд вернул код {status} без описания."
        return code, message

    # -- кэш врачей на диске ----------------------------------------------

    def _save_cache(self, docs: "list[Doctor]", source: str) -> None:
        payload = {
            "saved_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "source": source,
            "count": len(docs),
            "doctors": [doc.to_dict() for doc in docs],
        }
        try:
            path = Path(self.cache_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # кэш — не критичный для демо ресурс

    def _load_cache(self, *, require_source: "str | None" = None) -> "list[Doctor] | None":
        try:
            payload = json.loads(Path(self.cache_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if require_source and str(payload.get("source") or "") != require_source:
            return None
        rows = payload.get("doctors")
        if not isinstance(rows, list) or not rows:
            return None
        try:
            return [Doctor.from_api(row) for row in rows]
        except (KeyError, TypeError, ValueError):
            return None


__all__ = [
    "MisClient", "BASE_URL", "resolve_token", "mask_token",
    "RATE_LIMIT_REQUESTS", "RATE_LIMIT_WINDOW", "MAX_PAGE", "MAX_TIMEOUT",
    "TOKEN_FILE", "CACHE_FILE",
]

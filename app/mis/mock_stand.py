"""Оффлайн-стенд «Регистратура» — тот же контракт, что у реального REST API.

Зачем: у команды нет токена стенда, а демо и автотесты обязаны работать
целиком оффлайн. Этот модуль повторяет семантику стенда поверх локального
JSON-файла (по умолчанию ``var/mock_stand.json``) и отдаёт ошибки в том же
виде, что стенд: ``MisError(status, code, message)`` с русским текстом.

Источник истины — ``docs/openapi.json``. Повторённые правила:

* три статуса записи: «Запланирована», «Отменена», «Завершена»;
* терминальную («Отменена»/«Завершена») запись менять нельзя — 409
  «КОНФЛИКТ_СТАТУСА»; повторная отмена уже отменённой — 200 и карточка как
  есть (идемпотентность), как у стенда;
* к деактивированному врачу запись и перенос запрещены (400), а запись
  к деактивированному врачу допускает только перевод в «Отменена»;
* ключ клиента — фамилия + имя + дата рождения (с учётом регистра);
  повторная регистрация тем же ключом даёт 200 и прежнюю карточку, новая — 201;
* списки: ``limit`` 1..50 (по умолчанию 50), ``offset`` >= 0;
* имена и отчество приводятся: обрезка обрамляющих пробелов, схлопывание
  внутренних пробелов, нормальная форма Юникода NFC, управляющий символ — 400.

Данные — только вымышленные (требование кейса).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

try:  # обычный запуск из корня проекта
    from app.types import MisError, VisitStatus, fmt_datetime
except ImportError:  # pragma: no cover - запуск из каталога app/mis или app/web
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.types import MisError, VisitStatus, fmt_datetime

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = PROJECT_ROOT / "var" / "mock_stand.json"

# Коды ошибок стенда (docs/openapi.json, схема Error).
CODE_PARAM = "НЕВЕРНЫЙ_ПАРАМЕТР"
CODE_BODY = "НЕВЕРНЫЙ_ЗАПРОС"
CODE_NOT_FOUND = "НЕ_НАЙДЕНО"
CODE_CONFLICT = "КОНФЛИКТ_СТАТУСА"
CODE_INTERNAL = "ВНУТРЕННЯЯ_ОШИБКА"

STATUSES = tuple(s.value for s in VisitStatus)
PLANNED = VisitStatus.PLANNED.value
CANCELLED = VisitStatus.CANCELLED.value
COMPLETED = VisitStatus.COMPLETED.value

MAX_PAGE = 50
MAX_OFFSET = 9_007_199_254_740_991

_CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")
_DATE_RE = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})\s*$")
_DATETIME_RE = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})\s*$")
_CLIENT_RE = re.compile(r"^/clients/(\d+)$")
_VISIT_RE = re.compile(r"^/visits/(\d+)$")


# --------------------------------------------------------------------------
# Вспомогательные проверки значений (те же правила, что у стенда)
# --------------------------------------------------------------------------


def _clean_name(value: Any, field: str, *, max_len: int = 100) -> str:
    """Привести ФИО-поле: NFC, обрезка, схлопывание пробелов, лимит длины."""
    if not isinstance(value, str):
        raise MisError(
            f"Поле «{field}» обязательно и должно содержать непустое значение строкой.",
            400, CODE_BODY,
        )
    if _CONTROL_RE.search(value):
        raise MisError(f"Поле «{field}» содержит недопустимый управляющий символ.", 400, CODE_BODY)
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if len(text) > max_len:
        raise MisError(
            f"Значение поля «{field}» длиннее {max_len} символов после приведения.", 400, CODE_BODY
        )
    return text


def _clean_comment(value: Any) -> "str | None":
    """Комментарий: сохраняется как есть (может быть многострочным), null/пробелы — очистка."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise MisError("Поле «comment» должно быть строкой или null.", 400, CODE_BODY)
    if _CONTROL_RE.search(value):
        raise MisError("Поле «comment» содержит недопустимый управляющий символ.", 400, CODE_BODY)
    if len(value) > 1000:
        raise MisError("Значение поля «comment» длиннее 1000 символов.", 400, CODE_BODY)
    return value if value.strip() else None


def _check_date(value: Any, field: str = "birth_date") -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value or ""):
        raise MisError(
            f"Поле «{field}» должно содержать дату в формате ДД.ММ.ГГГГ.", 400, CODE_BODY
        )
    m = _DATE_RE.match(value)
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        dt = datetime(year, month, day)
    except ValueError:
        raise MisError(f"Поле «{field}» содержит несуществующую дату.", 400, CODE_BODY)
    return dt.strftime("%d.%m.%Y")


def _check_datetime(value: Any, field: str = "scheduled_at") -> str:
    if not isinstance(value, str):
        raise MisError(f"Поле «{field}» обязательно и должно быть строкой.", 400, CODE_BODY)
    m = _DATETIME_RE.match(value)
    if not m:
        raise MisError(
            f"Поле «{field}» должно содержать момент в формате ДД.ММ.ГГГГ ЧЧ:ММ.", 400, CODE_BODY
        )
    day, month, year, hour, minute = (int(g) for g in m.groups())
    try:
        dt = datetime(year, month, day, hour, minute)
    except ValueError:
        raise MisError(f"Поле «{field}» содержит несуществующий момент времени.", 400, CODE_BODY)
    return dt.strftime("%d.%m.%Y %H:%M")


def _check_status(value: Any) -> str:
    if not isinstance(value, str):
        raise MisError("Поле «status» должно быть строкой.", 400, CODE_BODY)
    text = value.strip()
    if text not in STATUSES:
        raise MisError(
            "Поле «status» принимает значения «Запланирована», «Отменена» или «Завершена».",
            400, CODE_BODY,
        )
    return text


def _check_id(value: Any, field: str, *, in_body: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        code = CODE_BODY if in_body else CODE_PARAM
        raise MisError(f"Поле «{field}» обязано быть целым числом.", 400, code)
    if not (1 <= value <= 2_147_483_647):
        code = CODE_BODY if in_body else CODE_PARAM
        raise MisError(f"Поле «{field}» должно быть целым от 1 до 2147483647.", 400, code)
    return value


def _int_param(query: dict, name: str, default: int, lo: int, hi: int) -> int:
    if name not in query or query[name] is None:
        return default
    raw = query[name]
    if isinstance(raw, bool):
        raise MisError(f"Значение параметра «{name}» должно быть целым.", 400, CODE_PARAM)
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip()
        if text == "":
            raise MisError(
                f"Пустое значение параметра «{name}» — ошибка, а не значение по умолчанию.",
                400, CODE_PARAM,
            )
        try:
            value = int(text)
        except ValueError:
            raise MisError(f"Значение параметра «{name}» должно быть целым.", 400, CODE_PARAM)
    if not (lo <= value <= hi):
        raise MisError(
            f"Значение параметра «{name}» должно быть целым от {lo} до {hi}.", 400, CODE_PARAM
        )
    return value


def _page(rows: list, query: dict) -> list:
    limit = _int_param(query, "limit", MAX_PAGE, 1, MAX_PAGE)
    offset = _int_param(query, "offset", 0, 0, MAX_OFFSET)
    return rows[offset:offset + limit]


def _sort_key(visit: dict) -> tuple:
    """Порядок стенда: по возрастанию момента приёма, затем по id."""
    try:
        moment = _visit_date(visit)
    except ValueError:
        moment = datetime.min
    return (moment, int(visit.get("id", 0)))


def _as_date(text: str) -> datetime:
    return datetime.strptime(text, "%d.%m.%Y")


def _visit_date(visit: dict) -> datetime:
    return datetime.strptime(visit.get("scheduled_at", ""), "%d.%m.%Y %H:%M")


# --------------------------------------------------------------------------
# Оффлайн-стенд
# --------------------------------------------------------------------------


class MockStand:
    """Локальный стенд поверх JSON-файла.

    Основной вход — :meth:`handle` (как HTTP): ``handle(method, path, query, body)``
    возвращает ``(status, payload)`` и выбрасывает :class:`MisError` при отказе.
    """

    def __init__(self, path: "str | Path | None" = None, *, seed: bool = True,
                 clock: "Callable[[], datetime] | None" = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self._lock = threading.RLock()
        self._now: Callable[[], datetime] = clock or datetime.now
        self.data = self._load(seed=seed)

    # -- хранилище ---------------------------------------------------------

    def _blank(self) -> dict:
        return {
            "version": 1,
            "generated_at": "",
            "source": "MOCK",
            "next_client_id": 1,
            "next_visit_id": 1,
            "doctors": [],
            "clients": [],
            "visits": [],
        }

    def _seed(self) -> dict:
        data = self._blank()
        now = self._now()
        data["next_client_id"] = 2
        data["next_visit_id"] = 3
        data["doctors"] = [
            {"id": 1, "full_name": "Кузнецова Анна Сергеевна", "specialty": "терапевт", "is_active": True},
            {"id": 2, "full_name": "Смирнов Игорь Петрович", "specialty": "терапевт", "is_active": True},
            {"id": 3, "full_name": "Иванова Мария Петровна", "specialty": "кардиолог", "is_active": True},
            {"id": 4, "full_name": "Петров Сергей Николаевич", "specialty": "невролог", "is_active": True},
            {"id": 5, "full_name": "Соколова Елена Ивановна", "specialty": "педиатр", "is_active": True},
            # деактивированный врач: запись к нему запрещена, отменить запись можно
            {"id": 6, "full_name": "Орлов Дмитрий Александрович", "specialty": "хирург", "is_active": False},
        ]
        data["clients"] = [
            {"id": 1, "last_name": "Иванов", "first_name": "Пётр", "middle_name": "Сергеевич",
             "birth_date": "17.03.1985", "comment": None,
             "created_at": fmt_datetime(now - timedelta(days=30))},
        ]
        data["visits"] = [
            {"id": 1, "client_id": 1, "doctor_id": 3, "status": PLANNED,
             "scheduled_at": fmt_datetime((now + timedelta(days=3)).replace(hour=10, minute=30, second=0, microsecond=0)),
             "created_at": fmt_datetime(now - timedelta(days=2)),
             "updated_at": fmt_datetime(now - timedelta(days=2))},
            {"id": 2, "client_id": 1, "doctor_id": 6, "status": PLANNED,
             "scheduled_at": fmt_datetime((now + timedelta(days=5)).replace(hour=9, minute=0, second=0, microsecond=0)),
             "created_at": fmt_datetime(now - timedelta(days=1)),
             "updated_at": fmt_datetime(now - timedelta(days=1))},
        ]
        return data

    def _load(self, *, seed: bool) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    base = self._blank()
                    base.update(data)
                    for key in ("doctors", "clients", "visits"):
                        if not isinstance(base.get(key), list):
                            base[key] = []
                    if not base.get("generated_at"):
                        base["generated_at"] = fmt_datetime(self._now())
                    return base
            except (OSError, ValueError):
                pass
        data = self._seed() if seed else self._blank()
        data["generated_at"] = fmt_datetime(self._now())
        self.data = data
        self.save()
        return data

    def save(self) -> None:
        """Записать состояние на диск (атомарно, с повторами на Windows).

        На Windows ``os.replace`` может получить отказ доступа, если целевой
        файл в этот момент открыт другим процессом (индексатор, антивирус,
        параллельная запись демо-сценария). Поэтому запись повторяется, а
        уникальное имя временного файла исключает столкновение двух потоков
        внутри одного процесса.
        """
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.data, ensure_ascii=False, indent=1)
            last_error: "Exception | None" = None
            for attempt in range(8):
                tmp = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.{attempt}.tmp")
                try:
                    tmp.write_text(payload, encoding="utf-8")
                    os.replace(tmp, self.path)
                    return
                except OSError as exc:                      # WinError 5/32 — файл занят
                    last_error = exc
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    time.sleep(0.02 * (attempt + 1))
            if last_error is not None:
                raise last_error

    def reload(self) -> dict:
        """Перечитать файл (демо может править его вручную)."""
        with self._lock:
            self.data = self._load(seed=False)
            return self.data

    # -- поиск объектов ----------------------------------------------------

    def _doctor(self, doctor_id: int) -> dict:
        for row in self.data["doctors"]:
            if int(row["id"]) == int(doctor_id):
                return row
        raise MisError(
            f"Поле «doctor_id» указывает на врача, отсутствующего в справочнике: {doctor_id}.",
            400, CODE_BODY,
        )

    def _doctor_or_404(self, doctor_id: int) -> dict:
        for row in self.data["doctors"]:
            if int(row["id"]) == int(doctor_id):
                return row
        raise MisError(f"Врач с идентификатором «{doctor_id}» не найден.", 404, CODE_NOT_FOUND)

    def _client(self, client_id: int) -> dict:
        for row in self.data["clients"]:
            if int(row["id"]) == int(client_id):
                return row
        raise MisError(f"Клиент с идентификатором «{client_id}» не найден.", 404, CODE_NOT_FOUND)

    def _client_field(self, client_id: int) -> dict:
        """Клиент как значение поля (создание записи): отказ 400, а не 404."""
        for row in self.data["clients"]:
            if int(row["id"]) == int(client_id):
                return row
        raise MisError(
            f"Поле «client_id» указывает на клиента, отсутствующего у команды: {client_id}.",
            400, CODE_BODY,
        )

    def _visit(self, visit_id: int) -> dict:
        for row in self.data["visits"]:
            if int(row["id"]) == int(visit_id):
                return row
        raise MisError(f"Запись с идентификатором «{visit_id}» не найдена.", 404, CODE_NOT_FOUND)

    def _visit_out(self, visit: dict) -> dict:
        return {
            "id": int(visit["id"]),
            "client_id": int(visit["client_id"]),
            "doctor": dict(self._doctor_or_404(int(visit["doctor_id"]))),
            "scheduled_at": visit["scheduled_at"],
            "status": visit["status"],
            "created_at": visit.get("created_at", ""),
            "updated_at": visit.get("updated_at", ""),
        }

    # -- методы стенда -----------------------------------------------------

    def doctors(self, *, specialty: "str | None" = None) -> list[dict]:
        """Весь справочник (для удобства тестов и кэша)."""
        rows = sorted(self.data["doctors"], key=lambda r: int(r["id"]))
        if specialty and str(specialty).strip():
            needle = " ".join(str(specialty).split()).casefold()
            rows = [r for r in rows if str(r["specialty"]).casefold() == needle]
        return [dict(r) for r in rows]

    def doctors_page(self, query: dict) -> list[dict]:
        """Справочник с отбором по специальности и пагинацией limit/offset."""
        specialty = query.get("specialty")
        if specialty is not None and _CONTROL_RE.search(str(specialty)):
            raise MisError("Параметр «specialty» содержит недопустимый управляющий символ.",
                           400, CODE_PARAM)
        rows = self.doctors(specialty=specialty if isinstance(specialty, str) else None)
        return _page([dict(r) for r in rows], query)

    def create_client(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body, dict):
            raise MisError("Тело запроса должно быть объектом JSON.", 400, CODE_BODY)
        last = _clean_name(body.get("last_name"), "last_name")
        first = _clean_name(body.get("first_name"), "first_name")
        if not last:
            raise MisError(
                "Поле «last_name» обязательно и должно содержать непустое значение строкой.",
                400, CODE_BODY,
            )
        if not first:
            raise MisError(
                "Поле «first_name» обязательно и должно содержать непустое значение строкой.",
                400, CODE_BODY,
            )
        birth = _check_date(body.get("birth_date"))
        for row in self.data["clients"]:
            if (row["last_name"], row["first_name"], row["birth_date"]) == (last, first, birth):
                return 200, dict(row)  # карточка с этим ключом у токена уже была
        middle = None
        if body.get("middle_name") is not None:
            middle = _clean_name(body["middle_name"], "middle_name") or None
        comment = _clean_comment(body.get("comment"))
        client = {
            "id": int(self.data["next_client_id"]),
            "last_name": last, "first_name": first, "middle_name": middle,
            "birth_date": birth, "comment": comment,
            "created_at": fmt_datetime(self._now()),
        }
        self.data["next_client_id"] = client["id"] + 1
        self.data["clients"].append(client)
        self.save()
        return 201, dict(client)

    def list_clients(self, query: dict) -> list[dict]:
        rows = sorted(self.data["clients"], key=lambda r: int(r["id"]))
        for key in ("last_name", "first_name"):
            if query.get(key) is not None and str(query[key]).strip():
                needle = _clean_name(query[key], key)
                rows = [r for r in rows if r.get(key) == needle]
        if query.get("birth_date") is not None and str(query["birth_date"]).strip():
            needle = _check_date(query["birth_date"])
            rows = [r for r in rows if r.get("birth_date") == needle]
        return [dict(r) for r in _page(rows, query)]

    def get_client(self, client_id: int) -> dict:
        return dict(self._client(_check_id(client_id, "id")))

    def patch_client(self, client_id: int, body: dict) -> dict:
        if not isinstance(body, dict):
            raise MisError("Тело запроса должно быть объектом JSON.", 400, CODE_BODY)
        for forbidden in ("last_name", "first_name", "birth_date", "id"):
            if forbidden in body:
                raise MisError(
                    f"Поле «{forbidden}» образует ключ клиента и изменению не подлежит.",
                    400, CODE_BODY,
                )
        client = self._client(_check_id(client_id, "id"))
        if "middle_name" in body:
            raw = body["middle_name"]
            client["middle_name"] = None if raw is None or not str(raw).strip() \
                else _clean_name(raw, "middle_name")
        if "comment" in body:
            client["comment"] = _clean_comment(body["comment"])
        self.save()
        return dict(client)

    def create_visit(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise MisError("Тело запроса должно быть объектом JSON.", 400, CODE_BODY)
        client_id = _check_id(body.get("client_id"), "client_id")
        doctor_id = _check_id(body.get("doctor_id"), "doctor_id")
        scheduled_at = _check_datetime(body.get("scheduled_at"))
        self._client_field(client_id)
        doctor = self._doctor(doctor_id)
        if not doctor.get("is_active", True):
            raise MisError(
                "Поле «doctor_id» указывает на деактивированного врача: запись к нему невозможна.",
                400, CODE_BODY,
            )
        stamp = fmt_datetime(self._now())
        visit = {
            "id": int(self.data["next_visit_id"]), "client_id": client_id, "doctor_id": doctor_id,
            "scheduled_at": scheduled_at, "status": PLANNED,
            "created_at": stamp, "updated_at": stamp,
        }
        self.data["next_visit_id"] = visit["id"] + 1
        self.data["visits"].append(visit)
        self.save()
        return self._visit_out(visit)

    def list_visits(self, query: dict) -> list[dict]:
        rows = sorted(self.data["visits"], key=_sort_key)
        if query.get("client_id") is not None and str(query["client_id"]).strip():
            client_id = _int_param({"client_id": query["client_id"]}, "client_id", 0, 0, MAX_OFFSET)
            self._client(client_id)  # отсутствие клиента — 404, а не пустой список
            rows = [r for r in rows if int(r["client_id"]) == client_id]
        if query.get("doctor_id") is not None and str(query["doctor_id"]).strip():
            doctor_id = _int_param({"doctor_id": query["doctor_id"]}, "doctor_id", 0, 0, MAX_OFFSET)
            self._doctor_or_404(doctor_id)
            rows = [r for r in rows if int(r["doctor_id"]) == doctor_id]
        if query.get("status") is not None and str(query["status"]).strip():
            rows = [r for r in rows if r["status"] == _check_status(query["status"])]
        if query.get("date_from") is not None and str(query["date_from"]).strip():
            lo = _as_date(_check_date(query["date_from"], "date_from"))
            rows = [r for r in rows if _visit_date(r) >= lo]
        if query.get("date_to") is not None and str(query["date_to"]).strip():
            hi = _as_date(_check_date(query["date_to"], "date_to"))
            rows = [r for r in rows if _visit_date(r) <= hi]
        return [self._visit_out(r) for r in _page(rows, query)]

    def get_visit(self, visit_id: int) -> dict:
        return self._visit_out(self._visit(_check_id(visit_id, "id")))

    def patch_visit(self, visit_id: int, body: dict) -> dict:
        if not isinstance(body, dict):
            raise MisError("Тело запроса должно быть объектом JSON.", 400, CODE_BODY)
        visit = self._visit(_check_id(visit_id, "id"))
        if "client_id" in body:
            raise MisError(
                "Поле «client_id» изменению не подлежит: запись остаётся у клиента, которому создана.",
                400, CODE_BODY,
            )
        new_doctor_id = None
        if "doctor_id" in body:
            new_doctor_id = _check_id(body["doctor_id"], "doctor_id")
        new_scheduled = None
        if "scheduled_at" in body:
            new_scheduled = _check_datetime(body["scheduled_at"])
        new_status = None
        if "status" in body:
            new_status = _check_status(body["status"])
        changed = (
            (new_doctor_id is not None and new_doctor_id != int(visit["doctor_id"]))
            or (new_scheduled is not None and new_scheduled != visit["scheduled_at"])
            or (new_status is not None and new_status != visit["status"])
        )
        touched = any(k in body for k in ("doctor_id", "scheduled_at", "status"))

        # 1) Терминальная запись: меняться не может, повторная отмена — 200 как есть.
        if visit["status"] != PLANNED:
            if not touched or not changed:
                return self._visit_out(visit)
            raise MisError(
                f"Запись на приём в статусе «{visit['status']}» изменению не подлежит: "
                "перенос, смена врача и смена статуса невозможны.",
                409, CODE_CONFLICT,
            )

        # 2) Запись к деактивированному врачу: допустим только перевод в «Отменена».
        current = self._doctor_or_404(int(visit["doctor_id"]))
        if not current.get("is_active", True):
            pure_cancel = (
                new_status == CANCELLED
                and new_doctor_id is None
                and new_scheduled is None
            )
            if not changed:
                return self._visit_out(visit)
            if not pure_cancel:
                raise MisError(
                    "Врач записи на приём деактивирован: запись можно только отменить.",
                    409, CODE_CONFLICT,
                )

        # 3) Новый врач записи.
        if new_doctor_id is not None and new_doctor_id != int(visit["doctor_id"]):
            target = self._doctor(new_doctor_id)
            if not target.get("is_active", True):
                raise MisError(
                    "Поле «doctor_id» указывает на деактивированного врача: "
                    "запись к нему невозможна.",
                    400, CODE_BODY,
                )

        if not changed:
            return self._visit_out(visit)  # тело без изменений — updated_at не сдвигается
        if new_doctor_id is not None:
            visit["doctor_id"] = new_doctor_id
        if new_scheduled is not None:
            visit["scheduled_at"] = new_scheduled
        if new_status is not None:
            visit["status"] = new_status
        visit["updated_at"] = fmt_datetime(self._now())
        self.save()
        return self._visit_out(visit)

    # -- HTTP-подобная диспетчеризация -------------------------------------

    def handle(self, method: str, path: str, query: "dict | None" = None,
               body: "dict | None" = None) -> tuple[int, Any]:
        """Единая точка входа, повторяющая HTTP-контракт стенда."""
        method = (method or "GET").upper()
        query = dict(query or {})
        with self._lock:
            if method == "GET" and path == "/doctors":
                return 200, self.doctors_page(query)
            if method == "POST" and path == "/clients":
                return self.create_client(body)
            if method == "GET" and path == "/clients":
                return 200, self.list_clients(query)
            m = _CLIENT_RE.match(path)
            if m:
                client_id = int(m.group(1))
                if method == "GET":
                    return 200, self.get_client(client_id)
                if method == "PATCH":
                    return 200, self.patch_client(client_id, body)
            if method == "POST" and path == "/visits":
                return 201, self.create_visit(body)
            if method == "GET" and path == "/visits":
                return 200, self.list_visits(query)
            m = _VISIT_RE.match(path)
            if m:
                visit_id = int(m.group(1))
                if method == "GET":
                    return 200, self.get_visit(visit_id)
                if method == "PATCH":
                    return 200, self.patch_visit(visit_id, body)
        raise MisError(f"Метод {method} по пути «{path}» не существует.", 404, CODE_NOT_FOUND)


__all__ = [
    "MockStand", "DEFAULT_PATH", "MAX_PAGE",
    "CODE_PARAM", "CODE_BODY", "CODE_NOT_FOUND", "CODE_CONFLICT", "CODE_INTERNAL",
    "PLANNED", "CANCELLED", "COMPLETED", "STATUSES",
]

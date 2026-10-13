"""Самопроверка MIS-клиента и оффлайн-стенда «Регистратура».

Только стандартная библиотека. Запуск из корня проекта::

    python -X utf8 tests/test_mis.py

Что проверяется — см. ``docs/ARCHITECTURE.md`` §3.1 и отчёт по задаче task-2:
оффлайн-режим при пустом токене, кэш врачей (память + ``var/doctors_cache.json``),
пациенты, записи, деактивированный врач, 409 на терминальную запись, перенос по
R-06, пагинация по 50, ограничитель 10 запросов/мин, 429 + ``Retry-After``,
таймаут → ``MisTimeout``, 401 → оффлайн без падения, отсутствие ПДн в журнале.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.mis.client as client_mod  # noqa: E402
from app.mis.client import (  # noqa: E402
    CACHE_FILE,
    MAX_PAGE,
    MAX_TIMEOUT,
    RATE_LIMIT_REQUESTS,
    MisClient,
)
from app.mis.mock_stand import DEFAULT_PATH as MOCK_STAND_PATH  # noqa: E402
from app.mis.mock_stand import MockStand  # noqa: E402
from app.types import MisError, MisTimeout, VisitStatus  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="mis_tests_"))
_SEQ = iter(range(1, 10_000))


# --------------------------------------------------------------------------
# Мини-каркас тестов (без pytest, но совместим с ним)
# --------------------------------------------------------------------------


class SkipTest(Exception):
    """Тест не применим в этом окружении (например, нет сети)."""


TESTS: list = []


def test(fn):
    TESTS.append(fn)
    return fn


def run() -> int:
    print(f"MIS self-check, рабочая папка: {PROJECT_ROOT}")
    print(f"временные файлы: {TMP}\n")
    passed = failed = skipped = 0
    started = time.time()
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
        except SkipTest as exc:
            skipped += 1
            print(f"[ПРОПУЩЕН] {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 — тест-раннер обязан показать всё
            failed += 1
            print(f"[ОШИБКА]  {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"[ок]      {name}")
    print(f"\nитого: {passed} ок, {failed} ошибок, {skipped} пропущено, "
          f"{time.time() - started:.1f} с")
    return 1 if failed else 0


# --------------------------------------------------------------------------
# Вспомогательные средства
# --------------------------------------------------------------------------


@contextmanager
def hermetic():
    """Убрать MIS_TOKEN и var/token.txt, чтобы тесты не ходили в реальный стенд."""
    env = os.environ.pop("MIS_TOKEN", None)
    old_file = client_mod.TOKEN_FILE
    client_mod.TOKEN_FILE = TMP / "token_absent.txt"
    try:
        yield
    finally:
        client_mod.TOKEN_FILE = old_file
        if env is not None:
            os.environ["MIS_TOKEN"] = env


def paths() -> "tuple[Path, Path]":
    n = next(_SEQ)
    return TMP / f"mock_{n}.json", TMP / f"cache_{n}.json"


def offline_client(**kwargs) -> MisClient:
    """Клиент оффлайн-режима на отдельном файле стенда (var/mock_stand.json не трогаем)."""
    mock_path, cache_path = paths()
    with hermetic():
        client = MisClient(**kwargs)
    client.mock_path = mock_path
    client.cache_path = cache_path
    return client


def stub_client(handler, **kwargs) -> "tuple[MisClient, StubServer]":
    mock_path, cache_path = paths()
    server = StubServer(handler)
    client = MisClient(base_url=server.url, token=kwargs.pop("token", "test-token"), **kwargs)
    client.mock_path = mock_path
    client.cache_path = cache_path
    return client, server


def doctor_row(doctor_id: int = 1, *, active: bool = True) -> dict:
    return {"id": doctor_id, "full_name": f"Врач Номер {doctor_id}", "specialty": "терапевт",
            "is_active": active}


def visit_row(visit_id: int, client_id: int = 1, doctor_id: int = 1,
              when: str = "20.10.2026 10:30", status: str = VisitStatus.PLANNED.value) -> dict:
    return {"id": visit_id, "client_id": client_id, "doctor": doctor_row(doctor_id),
            "scheduled_at": when, "status": status,
            "created_at": "01.10.2026 09:00", "updated_at": "01.10.2026 09:00"}


class StubServer:
    """Мини-стенд на 127.0.0.1: поведение задаёт ``handler(method, path, query, body, headers)``.

    Обработчик возвращает ``(status, headers, payload)``; payload сериализуется в JSON.
    """

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list = []

        def dispatch(method, path, query, body, headers):
            route = path[len("/api/v1"):] if path.startswith("/api/v1") else path
            self.calls.append({"method": method, "path": route, "query": query, "body": body})
            return handler(method, route, query, body, headers)

        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _serve(self):  # noqa: N802
                try:
                    parsed = urlparse.urlparse(self.path)
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    body = json.loads(raw.decode("utf-8")) if raw else None
                    status, headers, payload = dispatch(
                        self.command, parsed.path, dict(urlparse.parse_qsl(parsed.query)),
                        body, dict(self.headers),
                    )
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(status)
                    for key, value in (headers or {}).items():
                        self.send_header(key, str(value))
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            do_GET = do_POST = do_PATCH = do_DELETE = _serve

            def log_message(self, *args):  # тишина в консоли тестов
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]
        _ = outer

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def close(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass

    def __enter__(self) -> "StubServer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FakeClock:
    """Управляемые часы и «сон»: ограничитель и 429 проверяются без реальных задержек."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.t += seconds

    def attach(self, client: MisClient) -> "FakeClock":
        client._now = self.now          # noqa: SLF001 — тестовый стенд-хук
        client._sleep = self.sleep      # noqa: SLF001
        return self


def expect_error(fn, status: int, code: str = "") -> MisError:
    try:
        fn()
    except MisError as exc:
        assert exc.status == status, f"ожидали статус {status}, получили {exc.status}: {exc}"
        if code:
            assert exc.code == code, f"ожидали код {code}, получили {exc.code}: {exc}"
        return exc
    raise AssertionError(f"ожидали MisError({status}), но вызов прошёл без ошибки")


# --------------------------------------------------------------------------
# 1-2. Оффлайн-режим, health, сценарий
# --------------------------------------------------------------------------


@test
def test_01_offline_without_token() -> None:
    client = offline_client(token="")
    assert client.offline is True, "без токена клиент обязан работать оффлайн"
    assert client.source == "MOCK"
    assert client.token_set is False
    assert client.offline_reason, "причина оффлайн-режима должна быть заполнена"

    doctors = client.get_doctors()
    assert doctors, "оффлайн-стенд должен отдавать справочник врачей"
    assert all(call.source == "MOCK" for call in client.calls), \
        [call.source for call in client.calls]
    assert client.calls[-1].status == 200 and client.calls[-1].ok
    assert client.requests == 0, "в оффлайн-режиме сетевых запросов быть не должно"


@test
def test_02_health_and_scenario() -> None:
    client = offline_client(token="")
    client.set_scenario("С-03")
    client.get_doctors()
    health = client.health()
    assert health["offline"] is True and health["source"] == "MOCK"
    assert health["rate_limit_per_min"] == 10
    assert health["timeout_limit"] == MAX_TIMEOUT == 5.0
    assert client.calls[-1].scenario == "С-03", "подпись сценария должна попадать в ApiCall"


# --------------------------------------------------------------------------
# 3-4. Кэш врачей
# --------------------------------------------------------------------------


@test
def test_03_doctors_cache_memory_and_disk() -> None:
    def handler(method, path, query, body, headers):
        assert headers.get("Authorization") == "Bearer test-token"
        if path != "/doctors":
            return 404, {}, {"code": "НЕ_НАЙДЕНО", "message": "нет"}
        offset = int(query.get("offset", 0))
        limit = int(query.get("limit", 50))
        rows = [doctor_row(i) for i in range(1, 4)]
        return 200, {}, rows[offset:offset + limit]

    client, server = stub_client(handler)
    try:
        doctors = client.get_doctors()
        assert [d.id for d in doctors] == [1, 2, 3]
        assert client.doctors_source == "MIS"
        requests_after_first = client.requests
        assert client.cache_path.exists(), "кэш врачей должен лежать на диске"
        payload = json.loads(client.cache_path.read_text(encoding="utf-8"))
        assert payload["source"] == "MIS" and payload["count"] == 3

        # повторный вызов — из памяти, без новых запросов
        assert len(client.get_doctors()) == 3
        assert client.requests == requests_after_first
        # фильтр по специальности считается локально
        assert [d.id for d in client.get_doctors(specialty="терапевт")] == [1, 2, 3]
        assert client.requests == requests_after_first
        # force=True — принудительное обновление
        client.get_doctors(force=True)
        assert client.requests > requests_after_first
    finally:
        server.close()


@test
def test_04_doctors_fallback_to_disk_cache() -> None:
    def handler(method, path, query, body, headers):
        return 200, {}, [doctor_row(1), doctor_row(2)]

    client, server = stub_client(handler)
    client.get_doctors()
    assert client.doctors_source == "MIS"
    assert client.cache_path.exists()
    server.close()  # стенд «упал»

    doctors = client.get_doctors(force=True)
    assert [d.id for d in doctors] == [1, 2], "при недоступности стенда — кэш с диска"
    assert client.doctors_source == "CACHE"
    assert client.offline is True, "недоступный стенд переводит клиент в оффлайн-режим"


# --------------------------------------------------------------------------
# 5-7. Пациенты
# --------------------------------------------------------------------------


@test
def test_05_patient_create_find_patch() -> None:
    client = offline_client(token="")
    patient = client.create_patient("Тестовый", "Тест", "01.01.1990",
                                    "Тестович", "Аллергия на пенициллин")
    assert patient.id > 0 and patient.full_name == "Тестовый Тест Тестович"
    assert client.calls[-1].method == "POST" and client.calls[-1].source == "MOCK"

    found = client.find_patient("Тестовый", "Тест", "01.01.1990")
    assert found is not None and found.id == patient.id
    assert client.find_patient("Тестовый", "Тест", "02.01.1990") is None
    assert client.search_clients(last_name="Тестовый")[0].id == patient.id
    assert client.get_patient(patient.id).birth_date == "01.01.1990"

    updated = client.patch_patient(patient.id, middle_name="Тестович-Младший", comment="")
    assert updated.middle_name == "Тестович-Младший" and updated.comment is None


@test
def test_06_duplicate_patient_key() -> None:
    client = offline_client(token="")
    first = client.create_patient("Иванов", "Пётр", "17.03.1985")
    again = client.create_patient("Иванов", "Пётр", "17.03.1985", comment="другое")
    assert again.id == first.id, "повторная регистрация тем же ключом не создаёт второго клиента"
    assert again.comment is None, "отчество и комментарий повторного запроса не применяются"
    assert len(client.search_clients(last_name="Иванов", first_name="Пётр",
                                     birth_date="17.03.1985")) == 1


@test
def test_07_local_validation_without_request() -> None:
    client = offline_client(token="")
    before = len(client.calls)
    expect_error(lambda: client.create_patient("", "Пётр", "17.03.1985"), 400, "НЕВЕРНЫЙ_ЗАПРОС")
    expect_error(lambda: client.create_patient("Иванов", "Пётр", "31.02.1990"), 400)
    expect_error(lambda: client.patch_patient(1, last_name="Петров"), 400, "НЕВЕРНЫЙ_ЗАПРОС")
    assert len(client.calls) == before, "локальный отказ не выдаёт себя за запрос к стенду"


# --------------------------------------------------------------------------
# 8-11. Записи на приём
# --------------------------------------------------------------------------


@test
def test_08_visit_create_cancel_409() -> None:
    client = offline_client(token="")
    patient = client.create_patient("Запись", "Тест", "05.05.1980")
    visit = client.create_visit(patient.id, 1, "20.10.2026 10:30")
    assert visit.status == VisitStatus.PLANNED.value
    assert visit.created_at == visit.updated_at
    assert client.get_visit(visit.id).id == visit.id
    assert [v.id for v in client.list_visits(client_id=patient.id)] == [visit.id]

    cancelled = client.cancel_visit(visit.id)
    assert cancelled.status == VisitStatus.CANCELLED.value
    again = client.cancel_visit(visit.id)
    assert again.status == VisitStatus.CANCELLED.value, "повторная отмена — 200 и карточка как есть"
    assert again.updated_at == cancelled.updated_at, "повторная отмена не двигает updated_at"

    exc = expect_error(lambda: client.patch_visit(visit.id, scheduled_at="21.10.2026 11:00"),
                       409, "КОНФЛИКТ_СТАТУСА")
    assert "Отменена" in exc.message

    completed = client.create_visit(patient.id, 2, "22.10.2026 12:00")
    client.patch_visit(completed.id, status=VisitStatus.COMPLETED.value)
    expect_error(lambda: client.patch_visit(completed.id, doctor_id=3), 409, "КОНФЛИКТ_СТАТУСА")
    expect_error(lambda: client.patch_visit(completed.id, status="Запланирована"), 409)
    expect_error(lambda: client.patch_visit(completed.id, status="Нет такого"), 400, "НЕВЕРНЫЙ_ЗАПРОС")


@test
def test_09_deactivated_doctor_forbidden() -> None:
    client = offline_client(token="")
    patient = client.create_patient("Врач", "Тест", "06.06.1975")
    exc = expect_error(lambda: client.create_visit(patient.id, 6, "20.10.2026 10:30"),
                       400, "НЕВЕРНЫЙ_ЗАПРОС")
    assert "деактивированного врача" in exc.message

    visit = client.create_visit(patient.id, 1, "20.10.2026 10:30")
    exc = expect_error(lambda: client.patch_visit(visit.id, doctor_id=6), 400, "НЕВЕРНЫЙ_ЗАПРОС")
    assert "деактивированного врача" in exc.message
    expect_error(lambda: client.create_visit(patient.id, 999, "20.10.2026 10:30"), 400)
    expect_error(lambda: client.create_visit(999, 1, "20.10.2026 10:30"), 400)


@test
def test_10_visit_to_deactivated_doctor_only_cancel() -> None:
    client = offline_client(token="")
    # в оффлайн-стенде уже есть запись Иванова (client_id=1) к деактивированному врачу 6
    visits = client.list_visits(client_id=1)
    deactivated = [v for v in visits if v.doctor.is_active is False]
    assert deactivated, "в засеянном стенде должна быть запись к деактивированному врачу"
    visit = deactivated[0]

    exc = expect_error(lambda: client.patch_visit(visit.id, scheduled_at="25.10.2026 08:00"),
                       409, "КОНФЛИКТ_СТАТУСА")
    assert "только отменить" in exc.message
    expect_error(lambda: client.patch_visit(visit.id, doctor_id=1), 409, "КОНФЛИКТ_СТАТУСА")
    expect_error(lambda: client.patch_visit(visit.id, status=VisitStatus.COMPLETED.value), 409)

    cancelled = client.cancel_visit(visit.id)
    assert cancelled.status == VisitStatus.CANCELLED.value


@test
def test_11_reschedule_new_then_cancel_old() -> None:
    client = offline_client(token="")
    patient = client.create_patient("Перенос", "Тест", "07.07.1970")
    old = client.create_visit(patient.id, 1, "20.10.2026 10:30")

    new = client.reschedule_visit(old.id, doctor_id=2, scheduled_at="21.10.2026 11:00")
    assert new.id != old.id and new.doctor.id == 2 and new.status == VisitStatus.PLANNED.value
    assert client.get_visit(old.id).status == VisitStatus.CANCELLED.value
    active = [v for v in client.list_visits(client_id=patient.id) if v.is_active]
    assert [v.id for v in active] == [new.id], "активной должна остаться только новая запись"

    expect_error(lambda: client.reschedule_visit(old.id, scheduled_at="22.10.2026 12:00"), 409)


# --------------------------------------------------------------------------
# 12, 21-22. Пагинация и фильтры
# --------------------------------------------------------------------------


@test
def test_12_visits_pagination_50_per_page() -> None:
    total = 120

    def handler(method, path, query, body, headers):
        if path != "/visits":
            return 404, {}, {"code": "НЕ_НАЙДЕНО", "message": "нет"}
        offset = int(query.get("offset", 0))
        limit = int(query.get("limit", 50))
        rows = [visit_row(i, when=f"{1 + i % 28:02d}.10.2026 09:00") for i in range(1, total + 1)]
        return 200, {}, rows[offset:offset + limit]

    client, server = stub_client(handler)
    client.probe_on_start = False
    try:
        visits = client.list_visits()
        assert len(visits) == total, f"ожидали {total} записей, получили {len(visits)}"
        pages = [call for call in server.calls if call["path"] == "/visits"]
        assert len(pages) == 3, f"страниц должно быть 3 (50+50+20), получили {len(pages)}"
        assert all(int(call["query"]["limit"]) == MAX_PAGE for call in pages)
        assert [int(call["query"]["offset"]) for call in pages] == [0, 50, 100]
        before = len(server.calls)
        assert len(client.list_visits()) == total, "короткий кэш отдаёт список без запросов"
        assert len(server.calls) == before
    finally:
        server.close()


@test
def test_21_visit_filters_and_unknown_client() -> None:
    client = offline_client(token="")
    assert len(client.list_visits(client_id=1)) == 2
    assert client.list_visits(client_id=1, status=VisitStatus.CANCELLED.value) == []
    assert client.list_visits(client_id=1, date_from="01.01.2000") != []
    assert client.list_visits(client_id=1, date_from="01.01.2035") == []
    expect_error(lambda: client.list_visits(client_id=999_999), 404, "НЕ_НАЙДЕНО")
    expect_error(lambda: client.get_patient(999_999), 404, "НЕ_НАЙДЕНО")


@test
def test_22_limit_above_50_rejected() -> None:
    client = offline_client(token="")
    stand = client.mock_stand
    expect_error(lambda: stand.handle("GET", "/doctors", {"limit": 51}), 400, "НЕВЕРНЫЙ_ПАРАМЕТР")
    expect_error(lambda: stand.handle("GET", "/doctors", {"limit": ""}), 400, "НЕВЕРНЫЙ_ПАРАМЕТР")
    status, rows = stand.handle("GET", "/doctors", {"limit": 2, "offset": 1})
    assert status == 200 and len(rows) == 2


# --------------------------------------------------------------------------
# 13-16. Лимит частоты и таймауты
# --------------------------------------------------------------------------


@test
def test_13_local_rate_limiter_waits() -> None:
    assert RATE_LIMIT_REQUESTS == 10
    client = offline_client(token="test-token")
    clock = FakeClock().attach(client)
    for _ in range(10):
        client._throttle()                       # noqa: SLF001
    assert client.rate_waits == 0, "первые 10 запросов проходят без ожидания"
    client._throttle()                           # noqa: SLF001 — 11-й обязан подождать
    assert client.rate_waits == 1
    assert 59.0 <= clock.slept <= 61.0, f"ожидание около минуты, получили {clock.slept}"
    client._throttle()                           # noqa: SLF001 — после ожидания окно свободно
    assert client.rate_waits == 1


@test
def test_14_429_retry_after() -> None:
    state = {"seen": 0}

    def handler(method, path, query, body, headers):
        state["seen"] += 1
        if state["seen"] == 1:
            return 429, {"Retry-After": "2"}, {"code": "ПРЕВЫШЕН_ЛИМИТ",
                                               "message": "Предел частоты запросов токена исчерпан."}
        return 200, {}, [doctor_row(1)]

    client, server = stub_client(handler)
    client.probe_on_start = False
    clock = FakeClock().attach(client)
    try:
        doctors = client.get_doctors(force=True)
        assert [d.id for d in doctors] == [1]
        assert client.retries_429 == 1, "повтор после 429 обязателен"
        assert clock.slept >= 2.0, f"ожидали ожидание по Retry-After, получили {clock.slept}"
        statuses = [call.status for call in client.calls]
        assert statuses == [429, 200], statuses
        assert client.calls[0].ok is False and client.calls[1].ok is True
    finally:
        server.close()


@test
def test_15_timeout_raises_mistimeout_then_offline() -> None:
    def handler(method, path, query, body, headers):
        time.sleep(1.0)
        return 200, {}, {"id": 1}

    client, server = stub_client(handler, timeout=0.2)
    client.probe_on_start = False
    try:
        started = time.perf_counter()
        try:
            client.create_visit(1, 1, "20.10.2026 10:30")
        except MisTimeout as exc:
            assert "не ответил" in exc.message
        else:
            raise AssertionError("таймаут обязан давать MisTimeout (основание R-06)")
        assert time.perf_counter() - started < 5.0, "клиент не должен ждать дольше предела"
        assert client.offline is True, "после таймаута клиент уходит в оффлайн-режим"
        assert client.fallbacks >= 1
    finally:
        server.close()

    # дальше демо продолжается на оффлайн-стенде, без падения
    visit = client.create_visit(1, 1, "20.10.2026 10:30")
    assert visit.id > 0 and visit.status == VisitStatus.PLANNED.value
    assert client.calls[-1].source == "MOCK"


@test
def test_16_timeout_limit_is_five_seconds() -> None:
    assert MAX_TIMEOUT == 5.0
    client = offline_client(token="test-token", timeout=30.0)
    assert client._effective_timeout() == 5.0          # noqa: SLF001
    other = offline_client(token="test-token", timeout=1.5)
    assert other._effective_timeout() == 1.5           # noqa: SLF001

    # ответ, пришедший позже предела, тоже считается таймаутом
    slow = offline_client(token="test-token")
    slow.probe_on_start = False
    slow._max_timeout = 0.2                             # noqa: SLF001
    slow._effective_timeout = lambda: 5.0               # noqa: SLF001 — отключаем сокет-таймаут
    slow._send = lambda *a, **k: (time.sleep(0.35) or (200, {}, b"[]"))  # noqa: SLF001
    exc = expect_error(lambda: slow._http_call("GET", "/doctors", note="проверка предела"), 0)
    assert "больше предела" in exc.message
    assert slow.last_error == exc.message


# --------------------------------------------------------------------------
# 17-20. 401, ПДн, локальный стенд, реальный стенд
# --------------------------------------------------------------------------


@test
def test_17_401_falls_back_to_mock() -> None:
    def handler(method, path, query, body, headers):
        return 401, {}, {"code": "НЕ_АВТОРИЗОВАН",
                         "message": "Токен команды не предъявлен, неизвестен или отозван."}

    client, server = stub_client(handler, token="bad-token")
    try:
        doctors = client.get_doctors()          # не должно быть исключения
        assert doctors, "после 401 клиент обязан отдать данные оффлайн-стенда"
        assert client.offline is True
        sources = [call.source for call in client.calls]
        assert "MIS" in sources, "отказ 401 должен быть виден в журнале"
        assert "MOCK" in sources, "переключение в оффлайн должно быть видно в журнале"
        first = client.calls[0]
        assert first.status == 401 and first.ok is False and first.source == "MIS"
        assert client.doctors_source == "MOCK"
        assert client.get_doctors(force=True), "дальше демо работает целиком оффлайн"
    finally:
        server.close()


@test
def test_18_no_pii_in_api_journal() -> None:
    client = offline_client(token="")
    patient = client.create_patient("Секретов", "Тайна", "11.02.1993",
                                    "Отчествович", "Аллергия на лидокаин")
    client.find_patient("Секретов", "Тайна", "11.02.1993")
    client.create_visit(patient.id, 1, "20.10.2026 10:30")
    client.patch_patient(patient.id, comment="Хронические болезни")

    dump = json.dumps([call.to_dict() for call in client.calls], ensure_ascii=False)
    for secret in ("Секретов", "Тайна", "Отчествович", "11.02.1993", "Аллергия", "лидокаин"):
        assert secret not in dump, f"ПДн «{secret}» попали в журнал ApiCall (152-ФЗ)"
    assert "***" in dump, "ПДн-поля должны быть замаскированы, а не исчезнуть бесследно"
    assert any(call.request.get("last_name") == "***" for call in client.calls)

    # ответы стенда тоже маскируются
    for call in client.calls:
        assert "birth_date" not in json.dumps(call.response, ensure_ascii=False) or \
            '"birth_date": "***"' in json.dumps(call.response, ensure_ascii=False)


@test
def test_19_local_stand_seed_is_fictional() -> None:
    stand = MockStand(MOCK_STAND_PATH)
    doctors = stand.doctors()
    assert len(doctors) >= 6, "в оффлайн-стенде должен быть справочник врачей"
    assert any(d["is_active"] is False for d in doctors), "нужен деактивированный врач"
    assert stand.data["clients"], "нужен хотя бы один демо-пациент"
    assert stand.data["visits"], "нужна хотя бы одна демо-запись"
    assert MOCK_STAND_PATH.exists()
    assert CACHE_FILE.parent == MOCK_STAND_PATH.parent, "кэш и стенд лежат в var/"


@test
def test_23_token_resolution_order() -> None:
    """Токен: аргумент → MIS_TOKEN → var/token.txt; нигде нет → оффлайн."""
    token_file = TMP / "token_dir" / "token.txt"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("# токен команды\n\nfile-token\n", encoding="utf-8")
    old_file = client_mod.TOKEN_FILE
    old_env = os.environ.pop("MIS_TOKEN", None)
    client_mod.TOKEN_FILE = token_file
    try:
        assert client_mod.resolve_token("") == "file-token"
        assert client_mod.resolve_token("explicit-token") == "explicit-token"
        os.environ["MIS_TOKEN"] = "env-token"
        assert client_mod.resolve_token("") == "env-token", "MIS_TOKEN приоритетнее файла"
        assert client_mod.resolve_token("explicit-token") == "explicit-token", \
            "явный аргумент приоритетнее всего"
        client = MisClient(base_url="http://127.0.0.1:1/api/v1")
        assert client.token_set is True and client.offline is False
        assert client.reload_token() is False, "токен не менялся — перезагрузка не нужна"
    finally:
        client_mod.TOKEN_FILE = old_file
        os.environ.pop("MIS_TOKEN", None)
        if old_env is not None:
            os.environ["MIS_TOKEN"] = old_env

    with hermetic():
        without = MisClient()
    assert without.offline is True and without.token_set is False
    # без заголовка Authorization реальный стенд отвечает 401, а клиент не ходит в сеть
    assert client_mod.mask_token("abcdefghijkl") == "abcd…kl"
    assert client_mod.mask_token("") == "" and client_mod.mask_token("короткий") == "…"


@test
def test_20_real_stand_requires_token() -> None:
    url = f"{client_mod.BASE_URL}/doctors"
    req = urlrequest.Request(url, headers={"Accept": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            raise AssertionError(f"стенд без токена ответил {resp.status}, ожидали 401")
    except urlerror.HTTPError as exc:
        assert exc.code == 401, f"без токена ожидали 401, получили {exc.code}"
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise SkipTest(f"нет доступа к реальному стенду ({exc}) — проверка пропущена")

    with hermetic():
        client = MisClient(token="", base_url=client_mod.BASE_URL)
    assert client.offline is True, "без токена клиент не ходит в сеть, а работает оффлайн"


def _cleanup() -> None:
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = run()
    finally:
        _cleanup()
    sys.exit(code)

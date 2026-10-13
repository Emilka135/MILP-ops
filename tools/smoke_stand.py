"""Ручная проверка реального стенда «Регистратура» (когда появится токен).

Запуск из корня проекта::

    python -X utf8 tools/smoke_stand.py               # токен из MIS_TOKEN или var/token.txt
    python -X utf8 tools/smoke_stand.py --no-token    # проверить, что без токена стенд даёт 401
    python -X utf8 tools/smoke_stand.py --token XXX   # токен аргументом
    python -X utf8 tools/smoke_stand.py --base-url https://host/api/v1

Что делает при наличии токена (каждый шаг печатает код ответа):

1. GET /doctors — справочник врачей (кэшируется в var/doctors_cache.json);
2. POST /clients — регистрация вымышленного пациента «Тестовый Тест Тестович»;
3. POST /visits — запись к первому активному врачу;
4. PATCH /visits/{id} — отмена записи;
5. журнал ApiCall: код, источник (MIS/MOCK), длительность, подпись.

Скрипт ничего не удаляет: отмена переводит запись в статус «Отменена», как того
требует стенд. В стенд попадают только вымышленные данные.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.mis.client import BASE_URL, MisClient, mask_token, resolve_token  # noqa: E402
from app.types import MisError, VisitStatus, fmt_datetime  # noqa: E402

SMOKE_LAST_NAME = "Тестовый"
SMOKE_FIRST_NAME = "Тест"
SMOKE_MIDDLE_NAME = "Тестович"
SMOKE_BIRTH_DATE = "01.01.1990"


def probe_without_token(base_url: str) -> int:
    """GET /doctors без заголовка Authorization: стенд обязан ответить 401."""
    url = f"{base_url.rstrip('/')}/doctors"
    req = urlrequest.Request(url, headers={"Accept": "application/json"})
    print(f"[i] проверка без токена: GET {url}")
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            print(f"[!] стенд ответил {resp.status} без токена — ожидали 401")
            return 1
    except urlerror.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print(f"[+] код ответа без токена: {exc.code} (ожидали 401)")
        if body:
            print(f"    тело: {body}")
        return 0 if exc.code == 401 else 1
    except Exception as exc:  # noqa: BLE001 — ручная проверка, показываем причину
        print(f"[!] стенд недоступен: {type(exc).__name__}: {exc}")
        return 2


def show(label: str, value: str) -> None:
    print(f"    {label}: {value}")


def print_journal(client: MisClient) -> None:
    print("\n[i] журнал ApiCall (без ПДн — только идентификаторы):")
    for call in client.calls:
        flag = "ок " if call.ok else "!  "
        print(f"    {flag}{call.ts}  {call.method:5} {call.path:16} "
              f"код={call.status or '—':>4}  {call.source:4}  {call.duration_ms:>5} мс  "
              f"{call.note}{(' | ' + call.error) if call.error else ''}")


def serve_mock(port: int) -> int:
    """Поднять оффлайн-стенд по HTTP — проверить «боевой» путь клиента без токена.

    ``python tools/smoke_stand.py --serve-mock 8791``
    затем в другом окне: ``python tools/smoke_stand.py --token test --base-url http://127.0.0.1:8791/api/v1``
    """
    import json as _json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qsl, urlparse

    from app.mis.mock_stand import MockStand

    stand = MockStand()
    print(f"[i] оффлайн-стенд по HTTP: http://127.0.0.1:{port}/api/v1 (файл {stand.path})")
    print("    без заголовка Authorization отвечает 401, как реальный стенд")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _serve(self):  # noqa: N802
            try:
                parsed = urlparse(self.path)
                route = parsed.path[len("/api/v1"):] if parsed.path.startswith("/api/v1") else parsed.path
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = _json.loads(raw.decode("utf-8")) if raw else None
                if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                    status, payload = 401, {"code": "НЕ_АВТОРИЗОВАН",
                                            "message": "Требуется действующий токен команды."}
                else:
                    try:
                        status, payload = stand.handle(
                            self.command, route, dict(parse_qsl(parsed.query)), body)
                    except MisError as exc:
                        status = exc.status or 400
                        payload = {"code": exc.code, "message": exc.message}
                data = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        do_GET = do_POST = do_PATCH = _serve

        def log_message(self, fmt, *args):
            print("    [http]", self.command, self.path, "→", fmt % args)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] остановлено")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка реального стенда «Регистратура»")
    parser.add_argument("--base-url", default=BASE_URL, help=f"базовый путь API (по умолчанию {BASE_URL})")
    parser.add_argument("--token", default="", help="токен команды (иначе MIS_TOKEN, затем var/token.txt)")
    parser.add_argument("--no-token", action="store_true",
                        help="только проверить, что без токена стенд отвечает 401")
    parser.add_argument("--serve-mock", type=int, metavar="PORT", default=0,
                        help="поднять оффлайн-стенд по HTTP на порту PORT (проверка боевого пути)")
    args = parser.parse_args(argv)

    if args.serve_mock:
        return serve_mock(args.serve_mock)

    print("=" * 78)
    print("Проверка стенда «Регистратура»")
    print("=" * 78)

    if args.no_token:
        return probe_without_token(args.base_url)

    token = args.token.strip() or resolve_token()
    if not token:
        print("[!] токен не найден: нет аргумента --token, переменной MIS_TOKEN и файла var/token.txt")
        print("    выпустите токен в панели организаторов и положите его в var/token.txt")
        code = probe_without_token(args.base_url)
        return 2 if code == 0 else code

    print(f"[i] базовый путь: {args.base_url}")
    show("токен", mask_token(token))

    client = MisClient(token=token, base_url=args.base_url, mock=False)
    client.set_scenario("SMOKE")
    steps: "list[tuple[str, int, bool]]" = []

    def note_step(label: str, code: int, expect_ok: bool = True) -> None:
        steps.append((label, code, (200 <= code < 300) if expect_ok else code == 409))

    # 1. Справочник врачей.
    print("\n[1] GET /doctors — справочник врачей")
    doctors = client.get_doctors(force=True)
    active = [d for d in doctors if d.is_active]
    note_step("GET /doctors", client.calls[-1].status)
    show("врачей всего", str(len(doctors)))
    for doc in doctors[:5]:
        show(f"  id={doc.id}", f"{doc.specialty}, активен={doc.is_active}")
    if not active:
        print("[!] в справочнике нет активных врачей — запись создать нельзя")
        print_journal(client)
        return 1
    if client.offline:
        print(f"[!] стенд недоступен, клиент ушёл в оффлайн-режим: {client.offline_reason}")
        print("    проверка боевого пути не выполнена; демо при этом продолжает работать оффлайн")
        print_journal(client)
        return 2

    # 2. Регистрация пациента.
    print(f"\n[2] POST /clients — регистрация «{SMOKE_LAST_NAME} {SMOKE_FIRST_NAME} "
          f"{SMOKE_MIDDLE_NAME}» ({SMOKE_BIRTH_DATE})")
    patient = client.create_patient(SMOKE_LAST_NAME, SMOKE_FIRST_NAME, SMOKE_BIRTH_DATE,
                                    SMOKE_MIDDLE_NAME, "Проверка стенда, вымышленные данные")
    steps.append(("POST /clients", client.calls[-1].status, True))
    show("id пациента", str(patient.id))
    show("код ответа", f"{client.calls[-1].status} (201 — создан, 200 — карточка уже была)")

    # 3. Запись на приём.
    doctor = active[0]
    when = fmt_datetime((datetime.now() + timedelta(days=3)).replace(
        hour=10, minute=30, second=0, microsecond=0))
    print(f"\n[3] POST /visits — запись к врачу id={doctor.id} на {when}")
    visit = client.create_visit(patient.id, doctor.id, when)
    steps.append(("POST /visits", client.calls[-1].status, True))
    show("id записи", str(visit.id))
    show("статус", visit.status)

    # 4. Отмена записи.
    print(f"\n[4] PATCH /visits/{visit.id} — отмена записи")
    cancelled = client.cancel_visit(visit.id)
    steps.append((f"PATCH /visits/{visit.id}", client.calls[-1].status, True))
    show("статус", cancelled.status)
    assert cancelled.status == VisitStatus.CANCELLED.value, "отмена не применилась"

    # 5. Повторная отмена идемпотентна, изменение отменённой — 409.
    print(f"\n[5] PATCH /visits/{visit.id} — повторная отмена (ожидаем 200)")
    again = client.cancel_visit(visit.id)
    steps.append(("PATCH повторная отмена", client.calls[-1].status, True))
    show("статус", again.status)

    print(f"\n[6] PATCH /visits/{visit.id} — перенос отменённой (ожидаем 409)")
    other_time = fmt_datetime((datetime.now() + timedelta(days=4)).replace(
        hour=11, minute=0, second=0, microsecond=0))
    try:
        client.patch_visit(visit.id, scheduled_at=other_time)
    except MisError as exc:
        steps.append(("PATCH перенос отменённой (409)", exc.status, exc.status == 409))
        show("код", f"{exc.status} {exc.code}")
        show("сообщение", exc.message)
    else:
        steps.append(("PATCH перенос отменённой (409)", 200, False))
        print("[!] стенд разрешил изменить отменённую запись — расхождение с ТЗ")

    print_journal(client)

    print("\n[i] итоги по шагам:")
    ok = True
    for name, code, good in steps:
        ok = ok and good
        print(f"    {'ок ' if good else '!  '}{name:32} код={code}")
    show("источник данных", client.source)

    leak = json.dumps([call.to_dict() for call in client.calls], ensure_ascii=False)
    pii_free = all(secret not in leak for secret in (SMOKE_LAST_NAME, SMOKE_BIRTH_DATE,
                                                     SMOKE_MIDDLE_NAME))
    print(f"    {'ок ' if pii_free else '!  '}ПДн в журнале ApiCall: "
          f"{'не найдены' if pii_free else 'НАЙДЕНЫ'}")

    print("\n" + "=" * 78)
    print("ИТОГ: " + ("все шаги прошли" if ok and pii_free else "есть отклонения, см. выше"))
    print("=" * 78)
    return 0 if (ok and pii_free) else 1


if __name__ == "__main__":
    sys.exit(main())

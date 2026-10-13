#!/usr/bin/env python3
"""Единая точка входа: запуск демо и самотесты.

Примеры::

    python run.py                  # поднять демо на http://127.0.0.1:8000
    python run.py --port 8080
    python run.py --token <ТОКЕН>  # включить реальный стенд «Регистратура»
    python run.py --self-test      # прогнать все проверки и выйти
    python run.py --build-data     # пересобрать расписание и аналитику из выгрузок

Зависимостей, кроме стандартной библиотеки Python, нет.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
sys.path.insert(0, ROOT)

from app.env import describe as env_describe, load_env   # noqa: E402


def _run(script: str, *args: str) -> int:
    print(f"\n$ {os.path.relpath(script, ROOT)} {' '.join(args)}".rstrip())
    return subprocess.call([PYTHON, "-X", "utf8", script, *args], cwd=ROOT)


def show_config() -> None:
    """Показать, откуда берутся настройки (без раскрытия секретов)."""
    print("  Настройки окружения:")
    for key, value in env_describe().items():
        print(f"    {key:14} {value}")


def self_test(with_http: bool = False) -> int:
    """Все проверки проекта: разбор речи, слоты, МИС, safety, сценарии, приёмка."""
    checks = [
        ("Достижимость сценариев С-01…С-15", "tools/check_scenarios.py", ()),
        ("Разбор русских дат и времени", "tests/test_ru.py", ()),
        ("Слот-движок и расписание", "tests/test_slots.py", ()),
        ("MIS-клиент и оффлайн-стенд", "tests/test_mis.py", ()),
        ("Safety-слой и база знаний", "tests/test_safety.py", ()),
        ("Сквозные сценарии P0", "tests/smoke_engine.py", ()),
        ("16 контрольных диалогов", "tests/acceptance.py", ()),
    ]
    failed: list[str] = []
    for title, script, args in checks:
        path = os.path.join(ROOT, script)
        if not os.path.exists(path):
            print(f"  ПРОПУЩЕНО (нет файла): {script}")
            continue
        print("\n" + "=" * 74)
        print(f"  {title} — {script}")
        print("=" * 74)
        if _run(path, *args) != 0:
            failed.append(f"{title} ({script})")

    if with_http:
        code = _http_test()
        if code != 0:
            failed.append("HTTP-API (tests/test_http.py)")

    print("\n" + "=" * 74)
    if failed:
        print("  ПРОВАЛЕНО:")
        for item in failed:
            print(f"    - {item}")
        return 1
    print("  ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    print("=" * 74)
    return 0


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_test() -> int:
    """Поднять сервер на свободном порту, прогнать tests/test_http.py и закрыть."""
    import subprocess
    import time
    import urllib.error
    import urllib.request

    port = _free_port()
    print("\n" + "=" * 74)
    print(f"  HTTP-API и демо-эндпоинты — порт {port}")
    print("=" * 74)
    server = subprocess.Popen(
        [PYTHON, "-X", "utf8", os.path.join(ROOT, "app", "web", "server.py"), "--port", str(port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(40):
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        else:
            print("  Сервер не поднялся — пропускаю HTTP-проверки")
            return 1
        return _run(os.path.join(ROOT, "tests", "test_http.py"), "--base", base)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def build_data() -> int:
    code = _run(os.path.join(ROOT, "tools", "build_schedule.py"))
    code |= _run(os.path.join(ROOT, "tools", "analyze_data.py"))
    return code


def serve(port: int, host: str, token: str, base_url: str, horizon: int) -> int:
    script = os.path.join(ROOT, "app", "web", "server.py")
    args = [script, "--port", str(port), "--host", host, "--horizon", str(horizon)]
    if token:
        args += ["--token", token]
    if base_url:
        args += ["--base-url", base_url]
    return _run(script, *args[1:])


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description="Голосовой ассистент регистратуры — демо")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--token", default="", help="токен команды для стенда «Регистратура»")
    parser.add_argument("--base-url", default="", help="базовый URL стенда (по умолчанию из спецификации)")
    parser.add_argument("--horizon", type=int, default=int(os.environ.get("HORIZON_DAYS", 14)),
                        help="горизонт записи, дней")
    parser.add_argument("--self-test", action="store_true", help="прогнать проверки и выйти")
    parser.add_argument("--with-http", action="store_true",
                        help="в самотестах дополнительно поднять сервер и проверить HTTP-API")
    parser.add_argument("--build-data", action="store_true", help="пересобрать data/* из выгрузок")
    parser.add_argument("--env", action="store_true", help="показать настройки окружения и выйти")
    args = parser.parse_args()

    if args.env:
        show_config()
        return 0
    if args.self_test:
        show_config()
        return self_test(with_http=args.with_http)
    if args.build_data:
        return build_data()
    return serve(args.port, args.host, args.token, args.base_url, args.horizon)


if __name__ == "__main__":
    sys.exit(main())

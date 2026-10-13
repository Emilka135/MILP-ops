"""Загрузка переменных окружения из файла ``.env`` без внешних зависимостей.

Порядок разрешения настроек в проекте: аргумент командной строки → переменная
окружения → файл ``.env`` → ``var/token.txt`` → значение по умолчанию.

Файл ``.env`` не коммитится (см. ``.gitignore``): в нём токен команды.
Шаблон — ``.env.example``.

Реализация намеренно минимальна: ``KEY=VALUE``, комментарии ``#``, опциональные
кавычки, поддержка ``export KEY=VALUE``. Значения не перезаписывают уже
установленные переменные окружения — окружение всегда важнее файла.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

__all__ = ["load_env", "env", "env_path"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_LOADED = False


def env_path(root: Optional[Path] = None) -> Path:
    """Путь к файлу ``.env`` (можно переопределить через ``DSH_ENV_FILE``)."""
    override = os.environ.get("DSH_ENV_FILE")
    if override:
        return Path(override)
    return (root or PROJECT_ROOT) / ".env"


def _parse_line(line: str) -> Optional[tuple[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.lower().startswith("export "):
        line = line[7:].strip()
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    else:
        # Незакавыченное значение: отбрасываем комментарий после пробела
        value = value.split(" #", 1)[0].strip()
    return key, value


def load_env(path: Optional[Path] = None, *, override: bool = False) -> dict[str, str]:
    """Прочитать ``.env`` и положить значения в ``os.environ``.

    Возвращает только те пары, которые реально применились. Повторные вызовы
    не перечитывают файл, если только не передан явный ``path``.
    """
    global _ENV_LOADED
    target = path or env_path()
    if _ENV_LOADED and path is None:
        return {}
    _ENV_LOADED = True

    applied: dict[str, str] = {}
    if not target.exists():
        return applied
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except OSError:
        return applied

    for line in raw.splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def env(name: str, default: str = "") -> str:
    """Значение настройки: окружение (с предварительной загрузкой ``.env``)."""
    load_env()
    return (os.environ.get(name) or default).strip()


def describe() -> dict[str, str]:
    """Что настроено — для ``--self-test`` и диагностики запуска.

    Секреты не раскрываются: показывается только факт наличия и длина.
    """
    load_env()
    token = env("MIS_TOKEN")
    path = env_path()
    return {
        "env_file": str(path) if path.exists() else "(нет)",
        "mis_token": f"задан ({len(token)} символов)" if token else "не задан",
        "mis_base_url": env("MIS_BASE_URL") or "(по умолчанию из спецификации)",
        "token_file": "есть" if (PROJECT_ROOT / "var" / "token.txt").exists() else "(нет)",
    }


if __name__ == "__main__":                       # python -m app.env
    for key, value in describe().items():
        print(f"{key:16} {value}")

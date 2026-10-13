"""Восстановление выгрузок заказчика в ``data/raw/``.

Выгрузки — это 7,5 МБ CSV, они не коммитятся (см. ``.gitignore``). Скрипт
копирует их из папки загрузок, чтобы проект можно было пересобрать на чистой
машине::

    python tools/fetch_data.py                  # ищет файлы в загрузках
    python tools/fetch_data.py --from D:\\dump  # или укажет папку явно
    python tools/fetch_data.py --check          # только проверить, что есть

Ожидаемые файлы (имя — как в папке загрузок или любое с подходящим содержимым):

* история записей на приём — 35 470 строк, колонки
  ``ID записи; Пациент (хэш); Дата приёма; Время приёма; Специалист; Специальность; Статус``;
* журнал звонков — 17 772 строки, колонки
  ``call_id; datetime; wait_sec; duration_sec; result; operator_ext; caller_number``.

Файлы распознаются по заголовку, а не по имени: организаторы выдают их с
UUID-именами. После копирования пересоберите данные::

    python run.py --build-data
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"

APPOINTMENTS_HEADER = "ID записи;Пациент"
CALLS_HEADER = "call_id;datetime"
TARGETS = {
    "appointments": ("appointments.csv", APPOINTMENTS_HEADER),
    "calls": ("calls.csv", CALLS_HEADER),
}


def _peek(path: Path, limit: int = 400) -> str:
    """Первые байты файла как текст (с учётом BOM и UTF-8)."""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _identify(path: Path) -> str | None:
    """Определить, что это за выгрузка, по заголовку. None — не наша."""
    head = _peek(path).replace("\ufeff", "")
    if APPOINTMENTS_HEADER in head:
        return "appointments"
    if CALLS_HEADER in head:
        return "calls"
    return None


def _candidate_dirs(extra: list[str]) -> list[Path]:
    dirs: list[Path] = []
    for item in extra:
        dirs.append(Path(item))
    home = Path.home()
    for name in ("Downloads", "Загрузки", "Desktop", "Рабочий стол"):
        candidate = home / name
        if candidate.is_dir():
            dirs.append(candidate)
    return [d for d in dirs if d.is_dir()]


def check() -> int:
    print(f"Каталог выгрузок: {RAW_DIR}")
    missing = []
    for kind, (filename, _header) in TARGETS.items():
        target = RAW_DIR / filename
        if target.exists() and target.stat().st_size > 0:
            size = target.stat().st_size / 1024
            print(f"  ЕСТЬ  {filename} ({size:.0f} КБ)")
        else:
            missing.append(filename)
            print(f"  НЕТ   {filename}")
    if missing:
        print("\nВыгрузок нет. Запустите: python tools/fetch_data.py")
        print("Без них работают демо и все тесты, но не пересобираются "
              "data/schedule_grid.json и data/metrics.json.")
        return 1
    print("\nВсё на месте.")
    return 0


def fetch(extra_dirs: list[str], *, overwrite: bool = False) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}

    for kind, (filename, _header) in TARGETS.items():
        target = RAW_DIR / filename
        if target.exists() and target.stat().st_size > 0 and not overwrite:
            found[kind] = target
            print(f"  уже есть: {target.relative_to(ROOT)}")

    search_dirs = _candidate_dirs(extra_dirs)
    if not search_dirs:
        print("Не нашёл ни одной папки для поиска. Укажите её: --from <путь>")
        return 1

    print(f"Ищу выгрузки в: {', '.join(str(d) for d in search_dirs)}")
    for directory in search_dirs:
        try:
            files = sorted(directory.glob("*.csv"))
        except OSError:
            continue
        for path in files:
            if path.name in {name for name, _ in TARGETS.values()}:
                continue
            kind = _identify(path)
            if kind is None or kind in found:
                continue
            target = RAW_DIR / TARGETS[kind][0]
            shutil.copyfile(path, target)
            found[kind] = target
            print(f"  скопировано: {path.name} → data/raw/{target.name} "
                  f"({target.stat().st_size / 1024:.0f} КБ)")

    missing = [name for kind, (name, _) in TARGETS.items() if kind not in found]
    if missing:
        print("\nНе найдены: " + ", ".join(missing))
        print("Укажите папку с выгрузками: python tools/fetch_data.py --from <путь>")
        return 1

    print("\nВсе выгрузки на месте. Пересоберите данные:")
    print("  python run.py --build-data")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Восстановить выгрузки в data/raw/")
    parser.add_argument("--from", dest="from_dirs", action="append", default=[],
                        help="папка с выгрузками (можно несколько раз)")
    parser.add_argument("--check", action="store_true", help="только проверить наличие")
    parser.add_argument("--force", action="store_true", help="перезаписать существующие файлы")
    args = parser.parse_args()

    if args.check:
        return check()
    return fetch(args.from_dirs, overwrite=args.force)


if __name__ == "__main__":
    sys.exit(main())

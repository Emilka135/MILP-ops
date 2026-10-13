"""Извлечение контракта между разметкой и скриптом интерфейса.

Нужен для безопасной переработки структуры: если поменять id или класс, которым
управляет JS, функционал демо ломается молча. Скрипт показывает, что именно
связано, и какие id/классы затронуты.

Запуск::

    python tools/check_frontend_contract.py            # отчёт
    python tools/check_frontend_contract.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "web" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    html, css, js = read("index.html"), read("styles.css"), read("app.js")

    html_ids = re.findall(r'id="([^"]+)"', html)
    js_ids = set(re.findall(r"[\"']#([a-zA-Z][\w-]*)[\"']", js))
    js_ids |= set(re.findall(r"getElementById\([\"']([\w-]+)[\"']", js))
    js_ids |= set(re.findall(r"querySelector\([\"']#([\w-]+)[\"']", js))
    # В интерфейсе есть хелперы, принимающие id первым аргументом:
    # $('dialog-feed'), setKv('u-intent', ...), setText('...', ...) и подобные.
    js_ids |= set(re.findall(r"\$\([\"']([\w-]+)[\"']\)", js))
    js_ids |= set(re.findall(
        r"\b(?:setKv|setText|setHtml|byId|fill|setValue|updateKv)\([\"']([\w-]+)[\"']", js))
    # Универсальная страховка: любые строковые литералы, совпадающие с id из HTML,
    # чтобы новая разметка не «потеряла» элемент из-за незнакомого хелпера.
    literals = set(re.findall(r"[\"']([\w][\w-]{2,})[\"']", js))
    js_ids |= {name for name in literals if name in set(html_ids)}

    html_classes: set[str] = set()
    for row in re.findall(r'class="([^"]+)"', html):
        html_classes.update(row.split())
    js_classes = set(re.findall(r"classList\.(?:add|remove|toggle|contains)\([\"']([\w-]+)", js))
    js_classes |= set(re.findall(r"[\"']\.([\w-]+)[\"']", js))

    endpoints = sorted(set(re.findall(r"[\"'](/api/[^\"'`]*)[\"']", js)))

    report = {
        "html_ids": html_ids,
        "html_ids_unique": sorted(set(html_ids)),
        "duplicate_html_ids": sorted({i for i in html_ids if html_ids.count(i) > 1}),
        "js_ids": sorted(js_ids),
        "js_ids_missing_in_html": sorted(js_ids - set(html_ids)),
        "html_ids_not_used_by_js": sorted(set(html_ids) - js_ids),
        "html_classes": sorted(html_classes),
        "js_classes_missing_in_html": sorted(js_classes - html_classes),
        "endpoints": endpoints,
        "lines": {
            "index.html": len(html.splitlines()),
            "styles.css": len(css.splitlines()),
            "app.js": len(js.splitlines()),
        },
    }

    print("=== Контракт разметки и скрипта ===")
    print(f"  id в HTML: {len(html_ids)} (уникальных {len(set(html_ids))})")
    print(f"  id, к которым обращается JS: {len(js_ids)}")
    print(f"  JS ищет id, которых нет в HTML: {report['js_ids_missing_in_html'] or 'нет'}")
    print(f"  дубли id в HTML: {report['duplicate_html_ids'] or 'нет'}")
    print(f"  id в HTML, не используемые JS ({len(report['html_ids_not_used_by_js'])}):")
    for item in report["html_ids_not_used_by_js"]:
        print(f"      {item}")
    print(f"  классы в HTML: {len(html_classes)}")
    print(f"  классы, которых JS ждёт, а в HTML нет: "
          f"{report['js_classes_missing_in_html'] or 'нет'}")
    print(f"\n=== Эндпоинты фронтенда ({len(endpoints)}) ===")
    for item in endpoints:
        print(f"  {item}")
    print("\n=== Объём ===")
    for name, count in report["lines"].items():
        print(f"  {name:12} {count} строк")

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\nОтчёт: {args.json}")

    return 1 if report["js_ids_missing_in_html"] or report["duplicate_html_ids"] else 0


if __name__ == "__main__":
    sys.exit(main())

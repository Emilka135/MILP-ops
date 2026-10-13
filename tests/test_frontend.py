#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмочная проверка интерфейса демо в headless Chrome.

Зачем: интерфейс перерабатывается (аккордеон, другое расположение панелей).
Проверка ловит именно поломку функционала, а не красоту:

* поднят ли демо-сервер и отдаёт ли ``/api/health``;
* отрендерился ли DOM в headless Chrome (а не пустой каркас);
* сохранён ли контракт разметки и скрипта: каждый id, к которому обращается
  ``app.js`` (по отчёту ``tools/check_frontend_contract.py``), есть в DOM;
* выполнился ли JS: кнопки сценариев построены, приветствие ассистента
  отрисовано в ``#dialog-feed``, ``#api-log`` показывает журнал или понятное
  пустое состояние, в панелях нет ``undefined``/``NaN``/``[object Object]``;
* нет ли ссылок на внешние ресурсы (демо должно работать оффлайн), отдаются ли
  локальные ``<script src>``/``<link href>`` и есть ли в DOM id, которые ищут
  сами скрипты (JS разбит на ``app.js`` и ``accordion.js``).

Только стандартная библиотека Python 3.12 + внешний Chrome. Пакеты не нужны.

Запуск::

    python -X utf8 tests/test_frontend.py
    python -X utf8 tests/test_frontend.py --json report.json
    python -X utf8 tests/test_frontend.py --url http://127.0.0.1:8000   # свой сервер

Код возврата: 0 — всё прошло (или Chrome не найден), 1 — есть падения.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

try:  # консоль Windows может быть не в UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_TOOL = ROOT / "tools" / "check_frontend_contract.py"
SERVER_SCRIPT = ROOT / "app" / "web" / "server.py"

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# Панели, в тексте которых не должно быть мусора сериализации.
PANELS_NO_GARBAGE = ("api-log", "u-params", "metrics-body", "transfer-body")
GARBAGE_TOKENS = ("undefined", "NaN", "[object Object]")

SCREENSHOTS = (("1440x900", "shot-1440x900.png"), ("1280x720", "shot-1280x720.png"))

HTTP_TIMEOUT = 5.0
HEALTH_TIMEOUT = 20.0
CHROME_TIMEOUT = 180


# --------------------------------------------------------------------------
# Мини-DOM: Chrome --dump-dom отдаёт сериализованный DOM, его хватает,
# чтобы найти элементы по id/классу и собрать текст.
# --------------------------------------------------------------------------

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
NO_TEXT_TAGS = {"script", "style", "template", "head"}


class Node:
    __slots__ = ("tag", "attrs", "parent", "children", "data")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node] = []
        self.data: list[str] = []

    @property
    def node_id(self) -> str:
        return self.attrs.get("id", "")

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def iter(self):
        yield self
        for child in self.children:
            yield from child.iter()

    def text(self) -> str:
        parts: list[str] = []

        def walk(node: Node) -> None:
            if node.tag in NO_TEXT_TAGS:
                return
            if node.data:
                parts.append("".join(node.data))
            for child in node.children:
                walk(child)

        walk(self)
        return re.sub(r"\s+", " ", "".join(parts)).strip()


class DomBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.stack: list[Node] = [self.root]

    def handle_starttag(self, tag, attrs) -> None:
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs) -> None:
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data) -> None:
        self.stack[-1].data.append(data)


def parse_dom(html: str) -> Node:
    builder = DomBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def find_by_id(root: Node, node_id: str) -> Node | None:
    for node in root.iter():
        if node.node_id == node_id:
            return node
    return None


def is_hidden(node: Node) -> bool:
    current: Node | None = node
    while current is not None:
        if "hidden" in current.attrs:
            return True
        style = (current.attrs.get("style") or "").replace(" ", "").lower()
        if "display:none" in style:
            return True
        current = current.parent
    return False


# --------------------------------------------------------------------------
# Инфраструктура: порт, сервер, Chrome.
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_chrome() -> str:
    candidates = [os.environ.get("CHROME_PATH", "")] + list(CHROME_CANDIDATES)
    candidates += [shutil.which(name) or "" for name in
                   ("chrome", "chrome.exe", "google-chrome", "chromium", "chromium-browser")]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def start_server(port: int, log_path: Path) -> subprocess.Popen:
    cmd = [sys.executable, "-X", "utf8", str(SERVER_SCRIPT), "--port", str(port)]
    log = open(log_path, "wb")
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.pop("MIS_TOKEN", None)          # оффлайн-стенд: проверка не должна ходить в сеть
    env.pop("MIS_BASE_URL", None)
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env)
    proc.log_file = log  # type: ignore[attr-defined]
    return proc


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
    log = getattr(proc, "log_file", None)
    if log is not None:
        try:
            log.close()
        except Exception:
            pass


def wait_health(base: str, timeout: float) -> tuple[bool, str]:
    url = f"{base}/api/health"
    deadline = time.monotonic() + timeout
    last = "нет ответа"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
                if response.status == 200:
                    return True, ""
                last = f"HTTP {response.status}"
        except Exception as exc:  # сервер ещё поднимается
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    return False, last


def post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def chrome_cmd(chrome: str, profile: Path, extra: list[str]) -> list[str]:
    return [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        f"--user-data-dir={profile}",
        "--virtual-time-budget=8000",
    ] + extra


def run_chrome(args: list[str]) -> subprocess.CompletedProcess:
    # ВАЖНО: вывод снимаем через PIPE; обычное перенаправление ">" на этой
    # машине даёт пустой файл.
    return subprocess.run(args, capture_output=True, timeout=CHROME_TIMEOUT)


def dump_dom(chrome: str, url: str, profiles: Path, report: dict) -> str:
    profile = profiles / f"dom-{int(time.time() * 1000)}"
    try:
        proc = run_chrome(chrome_cmd(chrome, profile, ["--dump-dom", url]))
        dom = proc.stdout.decode("utf-8", "replace")
        report["chrome_returncode"] = proc.returncode
        if len(dom) < 500:
            report["chrome_stderr_tail"] = proc.stderr.decode("utf-8", "replace")[-600:]
        return dom
    except subprocess.TimeoutExpired:
        report["chrome_stderr_tail"] = f"Chrome не ответил за {CHROME_TIMEOUT} с"
        return ""
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def make_screenshot(chrome: str, url: str, profiles: Path, out_path: Path,
                    size: str, attempts: int = 3) -> tuple[bool, str]:
    """Снимок экрана. Headless Chrome изредка не создаёт файл — пробуем несколько раз."""
    last = "неизвестная ошибка"
    for attempt in range(1, attempts + 1):
        profile = profiles / f"shot-{size}-{int(time.time() * 1000)}-{attempt}"
        try:
            out_path.unlink(missing_ok=True)
            proc = run_chrome(chrome_cmd(
                chrome, profile,
                [f"--window-size={size}", "--hide-scrollbars", f"--screenshot={out_path}", url]))
            if out_path.exists():
                size_bytes = out_path.stat().st_size
                if size_bytes >= 1000:
                    note = f"{size_bytes} Б"
                    if attempt > 1:
                        note += f" (попытка {attempt})"
                    return True, note
                last = f"файл подозрительно мал: {size_bytes} Б"
            else:
                error = proc.stderr.decode("utf-8", "replace").strip()
                last = (f"Chrome rc={proc.returncode}, файл не создан"
                        + (f", stderr: {error[-200:]}" if error else ", stderr пуст"))
        except subprocess.TimeoutExpired:
            last = f"Chrome не ответил за {CHROME_TIMEOUT} с"
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        time.sleep(0.5)
    return False, last


def read_contract(report: dict) -> dict:
    out_path = Path(report["temp_dir"]) / "contract.json"
    cmd = [sys.executable, "-X", "utf8", str(CONTRACT_TOOL), "--json", str(out_path)]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=60)
    if not out_path.exists():
        report["contract_error"] = proc.stderr.decode("utf-8", "replace")[-400:]
        return {}
    return json.loads(out_path.read_text(encoding="utf-8"))


# id, которые ищут локальные скрипты (однозначные шаблоны, без эвристик).
SCRIPT_ID_PATTERNS = (
    r"""getElementById\(\s*["']([\w-]+)["']""",
    r"""querySelector(?:All)?\(\s*["']#([\w-]+)["']""",
)


def collect_assets(root: Node) -> list[tuple[str, str]]:
    assets: list[tuple[str, str]] = []
    for node in root.iter():
        if node.tag == "script" and node.attrs.get("src"):
            assets.append(("script", node.attrs["src"].strip()))
        elif node.tag == "link" and node.attrs.get("href"):
            assets.append(("link", node.attrs["href"].strip()))
    return assets


def fetch_asset(base: str, ref: str) -> tuple[int, str]:
    url = urllib.parse.urljoin(base + "/", ref)
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
            body = response.read()
            return response.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:
        return 0, ""


def extract_script_ids(js: str) -> set[str]:
    found: set[str] = set()
    for pattern in SCRIPT_ID_PATTERNS:
        found |= set(re.findall(pattern, js))
    return found


# --------------------------------------------------------------------------
# Проверки.
# --------------------------------------------------------------------------

class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.items.append({"name": name, "ok": bool(ok), "detail": detail})
        mark = "OK  " if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)

    @property
    def passed(self) -> int:
        return sum(1 for item in self.items if item["ok"])

    @property
    def total(self) -> int:
        return len(self.items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Приёмка интерфейса демо в headless Chrome")
    parser.add_argument("--json", default="", help="путь машинночитаемого отчёта")
    parser.add_argument("--url", default="", help="проверить уже запущенный сервер")
    parser.add_argument("--port", type=int, default=0, help="порт демо-сервера (0 — свободный)")
    parser.add_argument("--out-dir", default="", help="куда положить скриншоты (по умолчанию temp)")
    parser.add_argument("--keep", action="store_true", help="не удалять временную папку")
    args = parser.parse_args(argv)

    report: dict = {
        "status": "ok",
        "checks": [],
        "passed": 0,
        "total": 0,
        "temp_dir": "",
        "screenshots": [],
        "limitations": [
            "Проверяется отрендеренный DOM, а не визуальная вёрстка: сдвиги и перекрытия "
            "панелей скриншотами не измеряются.",
            "Chrome отдаёт снимок после --virtual-time-budget=8000; асинхронные обновления "
            "позже этого окна не видны.",
            "Тексты проверяются подстрокой, поэтому переформулировка приветствия без потери "
            "смысла может потребовать правки эталона (приветствие берётся из живого /api/session).",
            "Элементы внутри <details>/<template> и скрытые панели считаются по DOM, "
            "видимость проверяется отдельно только для мусорных токенов.",
            "Скриншоты — единственная среда-зависимая проверка: headless Chrome изредка не "
            "создаёт файл, поэтому попытка повторяется (до 3 раз).",
            "Проверка id из локальных скриптов консервативна: берутся только однозначные "
            "getElementById('…')/querySelector('#…'). accordion.js работает на классах и "
            "data-slot, поэтому сейчас таких id 0; проверка сработает, когда новый скрипт "
            "начнёт искать элемент по id.",
            "data-slot/классовый контракт аккордеона (acc-trigger, acc-content) не проверяется: "
            "он держится на классах, а не на id, и ловится только косвенно — через выполнение JS.",
        ],
    }

    chrome = find_chrome()
    if not chrome:
        print("Chrome не найден — проверка интерфейса пропущена (это не падение).")
        print("Укажите путь через переменную окружения CHROME_PATH.")
        report["status"] = "skipped"
        report["skip_reason"] = "chrome not found"
        if args.json:
            Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        return 0

    report["chrome"] = chrome
    out_dir = Path(args.out_dir).resolve() if args.out_dir else None
    temp_dir = Path(tempfile.mkdtemp(prefix="frontend-accept-"))
    report["temp_dir"] = str(temp_dir)
    shots_dir = out_dir or (temp_dir / "shots")
    profiles = temp_dir / "profiles"
    shots_dir.mkdir(parents=True, exist_ok=True)
    profiles.mkdir(parents=True, exist_ok=True)

    checks = Checks()
    server: subprocess.Popen | None = None
    try:
        if args.url:
            base = args.url.rstrip("/")
        else:
            port = args.port or free_port()
            base = f"http://127.0.0.1:{port}"
            print(f"=== Поднимаю демо-сервер на {base} ===")
            server = start_server(port, temp_dir / "server.log")
        report["base_url"] = base
        healthy, why = wait_health(base, HEALTH_TIMEOUT)
        checks.add("сервер:/api/health отвечает 200", healthy,
                   base if healthy else f"{base} — {why}")

        greeting = ""
        try:
            greeting = str(post_json(f"{base}/api/session", {}).get("greeting") or "")
        except Exception as exc:
            report["greeting_error"] = f"{type(exc).__name__}: {exc}"
        report["expected_greeting"] = greeting

        print("=== Снимаю DOM и контракт разметки ===")
        dom = dump_dom(chrome, base + "/", profiles, report)
        report["dom_bytes"] = len(dom.encode("utf-8"))
        (temp_dir / "page-dom.html").write_text(dom, encoding="utf-8")
        checks.add("chrome:получен отрендеренный DOM",
                   len(dom) > 500,
                   f"{report['dom_bytes']} Б" if len(dom) > 500
                   else f"пусто/мало; stderr: {report.get('chrome_stderr_tail', '')[:200]}")

        root = parse_dom(dom)
        dom_ids = [node.node_id for node in root.iter() if node.node_id]
        dom_id_set = set(dom_ids)

        contract = read_contract(report)
        html_ids = contract.get("html_ids") or []
        js_ids = contract.get("js_ids") or []
        report["html_ids_count"] = len(html_ids)
        report["js_ids_count"] = len(js_ids)
        checks.add("контракт:отчёт tools/check_frontend_contract.py получен",
                   bool(html_ids) and bool(js_ids),
                   f"id в HTML: {len(html_ids)}, id для JS: {len(js_ids)}"
                   if html_ids and js_ids else report.get("contract_error", "нет данных"))

        missing = [name for name in js_ids if name not in dom_id_set]
        checks.add("разметка:каждый id из js_ids найден в DOM",
                   not missing,
                   "все найдены" if not missing else f"нет в DOM: {', '.join(sorted(missing))}")

        duplicates = sorted({name for name in html_ids if html_ids.count(name) > 1})
        checks.add("разметка:id в index.html уникальны",
                   not duplicates,
                   "дублей нет" if not duplicates else f"дубли: {', '.join(duplicates)}")

        dom_duplicates = sorted({name for name in dom_ids if dom_ids.count(name) > 1})
        checks.add("разметка:в отрендеренном DOM нет дублей id",
                   not dom_duplicates,
                   "дублей нет" if not dom_duplicates
                   else f"дубли в DOM: {', '.join(dom_duplicates)}")

        buttons = [node for node in root.iter()
                   if node.tag == "button" and "scenario-btn" in node.classes()]
        named = [button for button in buttons if button.text()]
        texts = [button.text() for button in named]
        checks.add("js:кнопок button.scenario-btn ≥ 9 с непустым текстом",
                   len(named) >= 9,
                   f"найдено {len(named)} из {len(buttons)}"
                   + ("" if len(named) >= 9 else " — JS не построил кнопки сценариев"))
        duplicates_text = sorted({text for text in texts if texts.count(text) > 1})
        checks.add("js:тексты кнопок сценариев различаются",
                   len(texts) >= 9 and not duplicates_text,
                   f"уникальных {len(set(texts))} из {len(texts)}"
                   if not duplicates_text else f"повторы: {duplicates_text[:3]}")

        feed = find_by_id(root, "dialog-feed")
        if feed is None:
            checks.add("js:в #dialog-feed отрисовано приветствие ассистента", False,
                       "контейнер #dialog-feed не найден")
        else:
            feed_text = feed.text()
            placeholder_left = any("empty" in node.classes() for node in feed.iter())
            greeting_probe = greeting.strip()[:40]
            matched = bool(greeting_probe) and greeting_probe in feed_text
            rendered = bool(feed_text) and not placeholder_left
            checks.add("js:в #dialog-feed отрисовано приветствие ассистента",
                       matched or rendered,
                       ("приветствие совпало с ответом /api/session" if matched
                        else "лента непустая, плейсхолдер заменён" if rendered
                        else f"текст: {feed_text[:120]!r}"))

        log_node = find_by_id(root, "api-log")
        log_state = "не найдена"
        log_ok = False
        if log_node is not None:
            rows = [node for node in log_node.iter() if node.tag == "tr"]
            data_rows = [row for row in rows if "api-empty" not in row.classes()]
            empty_rows = [row for row in rows if "api-empty" in row.classes()]
            if data_rows:
                log_ok, log_state = True, f"строк журнала: {len(data_rows)}"
            elif empty_rows and any(row.text() for row in empty_rows):
                log_ok, log_state = True, "понятное пустое состояние"
            else:
                log_state = f"tbody пуст (tr: {len(rows)}) — журнал не отрисован"
        checks.add("js:#api-log содержит строки журнала или понятное пустое состояние",
                   log_ok, log_state)
        report["api_log_state"] = log_state

        garbage: list[str] = []
        for panel_id in PANELS_NO_GARBAGE:
            panel = find_by_id(root, panel_id)
            if panel is None:
                garbage.append(f"#{panel_id} не найден")
                continue
            text = panel.text()
            for token in GARBAGE_TOKENS:
                if token in text:
                    index = text.find(token)
                    snippet = text[max(0, index - 40):index + 40]
                    hidden = " (панель скрыта)" if is_hidden(panel) else ""
                    garbage.append(f"#{panel_id} содержит {token!r}{hidden}: …{snippet}…")
        checks.add("js:в панелях нет undefined / NaN / [object Object]",
                   not garbage, "чисто" if not garbage else "; ".join(garbage[:4]))

        external: list[str] = []
        for node in root.iter():
            if node.tag == "script" and node.attrs.get("src"):
                src = node.attrs["src"].strip()
                if src.startswith(("http://", "https://", "//")):
                    external.append(f"<script src={src}>")
            if node.tag == "link" and node.attrs.get("href"):
                href = node.attrs["href"].strip()
                if href.startswith(("http://", "https://", "//")):
                    external.append(f"<link href={href}>")
        report["external_resources"] = external
        checks.add("сеть:нет внешних script src и link href",
                   not external, "внешних ресурсов нет" if not external else "; ".join(external[:4]))

        # Разметка перерабатывается, а JS уже разбит на несколько файлов: проверяем,
        # что каждый локальный <script src>/<link href> реально отдаётся сервером
        # (битая ссылка на accordion.js молча убивает функционал) и что id, которые
        # ищут эти скрипты, есть в DOM.
        assets = collect_assets(root)
        broken_assets: list[str] = []
        script_ids: set[str] = set()
        local_scripts = 0
        for kind, ref in assets:
            if not ref or ref.startswith(("http://", "https://", "//", "data:", "#")):
                continue
            status, body = fetch_asset(base, ref)
            if kind == "script":
                local_scripts += 1
                if status != 200 or len(body) < 200:
                    broken_assets.append(f"<script src={ref}> → HTTP {status}, {len(body)} симв.")
                else:
                    script_ids |= extract_script_ids(body)
            elif status != 200 or not body.strip():
                broken_assets.append(f"<link href={ref}> → HTTP {status}, {len(body)} симв.")
        report["local_assets"] = [{"kind": kind, "ref": ref} for kind, ref in assets]
        checks.add("сеть:локальные script src и link href отдаются с кодом 200",
                   not broken_assets,
                   f"проверено ссылок: {len(assets)}" if not broken_assets
                   else "; ".join(broken_assets[:4]))

        missing_in_scripts = sorted(script_ids - dom_id_set)
        checks.add("разметка:id, которые ищут локальные скрипты, есть в DOM",
                   not missing_in_scripts,
                   f"скриптов: {local_scripts}, id: {len(script_ids)} — все найдены"
                   if not missing_in_scripts
                   else f"нет в DOM: {', '.join(missing_in_scripts)}")

        print("=== Скриншоты ===")
        for size, filename in SCREENSHOTS:
            path = shots_dir / filename
            ok, detail = make_screenshot(chrome, base + "/", profiles, path, size)
            report["screenshots"].append(
                {"size": size, "path": str(path), "bytes": path.stat().st_size if path.exists() else 0,
                 "ok": ok})
            checks.add(f"скриншот {size}", ok, f"{path} ({detail})")

        report["checks"] = checks.items
        report["passed"] = checks.passed
        report["total"] = checks.total
        report["ok"] = checks.passed == checks.total
        report["status"] = "ok" if report["ok"] else "failed"
        print()
        print(f"ИТОГ: прошло {checks.passed} из {checks.total}")
        print(f"Скриншоты: {shots_dir}")
        if temp_dir.exists():
            print(f"DOM, контракт, лог сервера: {temp_dir}")
        else:
            print("Временные файлы удалены (--out-dir без --keep).")
        return 0 if report["ok"] else 1
    except Exception as exc:  # проверка не должна падать стектрейсом
        import traceback

        traceback.print_exc()
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["checks"] = checks.items
        report["passed"] = checks.passed
        report["total"] = checks.total
        report["ok"] = False
        print(f"\nИТОГ: прошло {checks.passed} из {checks.total} (ошибка проверки: {exc})")
        return 1
    finally:
        stop_server(server)
        if out_dir is not None and not args.keep:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if args.json:
            Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            print(f"Отчёт: {args.json}")


if __name__ == "__main__":
    sys.exit(main())

"""Visual scenario repository: validated DSL, revision checks, history and FTS5 search.

Only local project files are edited. Saving does not replace a running assistant;
optional builds produce a versioned binary for a normal service deployment.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import tempfile
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from calls.scenario.dsl import FlowError, parse_flow, validate

ROOT = Path(__file__).resolve().parents[3]
IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
LIMIT = 3 * 1024 * 1024


class Conflict(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ValueError(f"Недопустимый идентификатор: {value!r}")
    return value


def quote(value):
    text = str(value).replace("\r", " ").replace("\n", " ")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fields(args):
    return " ".join(identifier(k) + "=" + quote(v) for k, v in args.items())


def serialize(flow):
    name = identifier(flow["name"])
    rate = float(flow.get("rate", 1.5))
    if not 0.75 <= rate <= 2.5:
        raise ValueError("Скорость речи должна быть от 0,75 до 2,5")
    lines = [
        f"flow {name}",
        f"  rate {rate:g}",
        f"  entry {identifier(flow['entry'])}",
        "",
    ]
    for node in flow["states"]:
        lines.append("state " + identifier(node["name"]))
        for statement in node.get("statements", []):
            kind, args = statement["kind"], statement.get("args", {})
            if kind == "say":
                body = "say " + quote(statement.get("text", ""))
                if args:
                    body += " " + fields(args)
            elif kind == "ask":
                lower, upper = (
                    int(args.get("min", 1)),
                    int(args.get("max", args.get("min", 1))),
                )
                if not 1 <= lower <= upper <= 32:
                    raise ValueError("Длина цифрового ответа должна быть от 1 до 32")
                body = f"ask {identifier(args['var'])} digits {lower}-{upper} timeout {float(args.get('timeout', 25)):g}s"
                if args.get("terminator"):
                    body += " terminator " + quote(args["terminator"])
            elif kind == "listen":
                body = f"listen {float(args.get('timeout', 8)):g}s"
                if args.get("field"):
                    body += " field=" + identifier(args["field"])
            elif kind == "call":
                body = f"call {identifier(args['module'])}.{target_name(args['op'])}"
                parameters = {k[4:]: v for k, v in args.items() if k.startswith("arg:")}
                if parameters:
                    body += " " + fields(parameters)
            elif kind == "set":
                body = f"set {identifier(args['var'])} = {quote(args.get('value', ''))}"
            elif kind == "sms":
                body = "sms " + identifier(args["template"])
            elif kind == "transfer":
                body = "transfer " + fields(args)
            elif kind == "end":
                body = "end " + identifier(args.get("status", "done"))
            elif kind in ("goto", "enter"):
                target = target_name(args["target"])
                body = kind + " " + target
                if kind == "enter":
                    body += " -> " + target_name(args["return"])
            elif kind == "switch":
                body = "switch " + identifier(args["var"])
            elif kind == "return":
                body = "return"
            else:
                raise ValueError(f"Неизвестное действие: {kind}")
            lines.append("  " + body)
        for edge in node.get("edges", []):
            kind = identifier(edge["kind"])
            values = edge.get("values", [])
            body = kind
            if kind in ("dtmf", "intent", "case"):
                if not values or (kind == "case" and len(values) != 1):
                    raise ValueError(f"У перехода {kind} не задано условие")
                body += " " + quote("|".join(map(str, values)))
            elif kind == "timeout" and edge.get("seconds") is not None:
                body += f" {float(edge['seconds']):g}s"
            elif kind in ("loop", "retry"):
                body += f" {int(edge.get('count') or 1)}"
            body += " -> " + target_name(edge["target"])
            if edge.get("sets"):
                body += " " + fields(edge["sets"])
            lines.append("  " + body)
        lines.append("")
    return "\n".join(lines)


def target_name(value):
    return ".".join(identifier(part) for part in str(value).split("."))


def clean_flow(text, source="import.flow"):
    flow = parse_flow(text, source)
    return {
        "name": flow.name,
        "entry": flow.entry,
        "rate": flow.rate,
        "states": [dataclasses.asdict(s) for s in flow.states.values()],
    }


def compile_project(project):
    flows, sources, metadata = {}, {}, {}
    items = project.get("flows")
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError("Проект должен содержать от 1 до 50 сценариев")
    for item in items:
        if len(item.get("states", [])) > 1000:
            raise ValueError("В одном сценарии допускается не более 1000 блоков")
        source = serialize(item)
        name = item["name"]
        if name in flows:
            raise ValueError("Имена сценариев должны быть уникальными")
        flows[name] = parse_flow(source, name + ".flow")
        sources[name + ".flow"] = source
        metadata[name] = {
            "label": str(item.get("label", ""))[:200],
            "description": str(item.get("description", ""))[:2000],
            "states": {
                s["name"]: {
                    "label": str(s.get("label", ""))[:200],
                    "description": str(s.get("description", ""))[:2000],
                    "position": s.get("position"),
                }
                for s in item["states"]
            },
        }
    return sources, metadata, validate(flows)


class Repository:
    def __init__(self, root, data, allow_build=False):
        self.root, self.data = Path(root).resolve(), Path(data).resolve()
        self.folder = self.root / "scenarios"
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.build_lock = threading.Lock()
        self.allow_build = allow_build
        self.db = sqlite3.connect(self.data / "editor.sqlite", check_same_thread=False)
        self.db.executescript("""CREATE TABLE IF NOT EXISTS history(revision TEXT PRIMARY KEY, created REAL NOT NULL, project TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(key UNINDEXED, flow UNINDEXED, name, label, content, tokenize='unicode61', prefix='2 3 4');""")
        self.job = {"status": "idle"}
        if os.environ.get('EDITOR_BUILD_QUEUE') == '1':
            pending = list((self.data / 'build-jobs').glob('*.request.json')) + list((self.data / 'build-jobs').glob('*.running.json'))
            if pending:
                self.job = json.loads(max(pending, key=lambda p:p.stat().st_mtime).read_text())['job']
        with self.lock:
            self.reindex(self.read())

    def read(self):
        with self.lock:
            files = {
                p.name: p.read_text(encoding="utf-8")
                for p in sorted(self.folder.glob("*.flow"))
            }
            layout_path = self.folder / "editor-layout.json"
            layout_text = layout_path.read_text() if layout_path.exists() else "{}"
            layout = json.loads(layout_text)
            revision = hashlib.sha256(
                json.dumps([files, layout_text], sort_keys=True).encode()
            ).hexdigest()
            flows = []
            for filename, text in files.items():
                flow = clean_flow(text, filename)
                meta = layout.get(flow["name"], {})
                flow.update({k: v for k, v in meta.items() if k != "states"})
                for state in flow["states"]:
                    state.update(meta.get("states", {}).get(state["name"], {}))
                flows.append(flow)
            return {
                "flows": flows,
                "revision": revision,
                "build_enabled": self.allow_build,
            }

    def reindex(self, project):
        self.db.execute("DELETE FROM search")
        for flow in project["flows"]:
            for state in flow["states"]:
                content = json.dumps(state, ensure_ascii=False)
                self.db.execute(
                    "INSERT INTO search VALUES(?,?,?,?,?)",
                    (
                        flow["name"] + "." + state["name"],
                        flow["name"],
                        state["name"],
                        state.get("label", ""),
                        content,
                    ),
                )
        self.db.commit()

    def search(self, query):
        terms = re.findall(r"[^\W_]+", query, re.UNICODE)[:12]
        if not terms:
            return []
        match = " AND ".join('"' + t + '"*' for t in terms)
        with self.lock:
            rows = self.db.execute(
                "SELECT key,flow,name,label,snippet(search,4,'‹','›','…',18) FROM search WHERE search MATCH ? ORDER BY rank LIMIT 50",
                (match,),
            ).fetchall()
        return [
            dict(zip(["key", "flow", "name", "label", "excerpt"], row)) for row in rows
        ]

    def inspect(self, project):
        try:
            sources, _, errors = compile_project(project)
            return {"valid": not errors, "errors": errors, "files": sources}
        except (FlowError, ValueError, KeyError, TypeError, OverflowError) as exc:
            return {"valid": False, "errors": [str(exc)], "files": {}}

    def save(self, project, expected):
        sources, metadata, errors = compile_project(project)
        if errors:
            raise ValueError("\n".join(errors))
        with self.lock:
            before = self.read()
            if before["revision"] != expected:
                raise Conflict(
                    "Проект изменён в другом окне. Сначала загрузите свежую версию."
                )
            self.db.execute(
                "INSERT OR IGNORE INTO history VALUES(?,?,?)",
                (
                    before["revision"],
                    time.time(),
                    json.dumps(before, ensure_ascii=False),
                ),
            )
            contents = {**sources, "editor-layout.json": json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"}
            originals = {p.name: p.read_bytes() for p in self.folder.iterdir() if p.suffix == ".flow" or p.name == "editor-layout.json"}
            # Finish all writes before replacing any live source. Roll back failed installation.
            with tempfile.TemporaryDirectory(prefix=".save-", dir=self.folder) as staging:
                staged = Path(staging)
                for name, text in contents.items():
                    (staged / name).write_text(text, encoding="utf-8")
                try:
                    for name in contents:
                        os.replace(staged / name, self.folder / name)
                    for name in originals.keys() - contents.keys():
                        (self.folder / name).unlink()
                except OSError:
                    for name, raw in originals.items():
                        (self.folder / name).write_bytes(raw)
                    for name in contents.keys() - originals.keys():
                        (self.folder / name).unlink(missing_ok=True)
                    self.db.rollback()
                    raise
            after = self.read()
            self.db.execute(
                "INSERT OR IGNORE INTO history VALUES(?,?,?)",
                (after["revision"], time.time(), json.dumps(after, ensure_ascii=False)),
            )
            self.db.commit()
            self.reindex(after)
            return after

    def history(self):
        with self.lock:
            return [
                {"revision": r[0], "created": r[1]}
                for r in self.db.execute(
                    "SELECT revision,created FROM history ORDER BY created DESC LIMIT 50"
                )
            ]

    def restore(self, revision, expected):
        with self.lock:
            row = self.db.execute(
                "SELECT project FROM history WHERE revision=?", (revision,)
            ).fetchone()
            if row is None:
                raise ValueError("Версия не найдена")
            return self.save(json.loads(row[0]), expected)

    def build_status(self):
        if self.job.get('status') == 'running' and self.job.get('queued'):
            path = self.data / 'build-jobs' / (self.job['id'] + '.result.json')
            if path.exists():
                self.job = json.loads(path.read_text())
            elif time.time() - self.job['created'] > 600:
                self.job.update(status='failed', error='Сборщик не завершил задание за 10 минут. Проверьте отдельный сервис сборки.')
        return dict(self.job)

    def start_build(self, expected):
        if not self.allow_build:
            raise ValueError(
                "Сборка в этом редакторе выключена. Доступен экспорт проекта."
            )
        from .audio_release import require_audio_audit
        require_audio_audit(self.read(), self.root)
        if os.environ.get('EDITOR_BUILD_QUEUE') == '1':
            with self.lock:
                if self.build_status().get('status') == 'running':
                    raise Conflict('Сборка уже выполняется')
                project = self.read()
                if project['revision'] != expected:
                    raise Conflict('Сначала сохраните и обновите проект')
                job_id = uuid.uuid4().hex
                self.job = {'id':job_id, 'status':'running', 'queued':True, 'revision':expected, 'created':time.time(), 'log':'Ожидаем отдельный сборщик сценариев…'}
                jobs = self.data / 'build-jobs'
                jobs.mkdir(exist_ok=True)
                temp = jobs / (job_id + '.tmp')
                temp.write_text(json.dumps({'job':self.job, 'project':project}, ensure_ascii=False))
                os.replace(temp, jobs / (job_id + '.request.json'))
                return dict(self.job)
        if not self.build_lock.acquire(blocking=False):
            raise Conflict("Сборка уже выполняется")
        if self.read()["revision"] != expected:
            self.build_lock.release()
            raise Conflict("Сначала сохраните и обновите проект")
        self.job = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "revision": expected,
            "log": "",
        }

        def work():
            job = self.job
            try:
                with self.lock:
                    log_path = self.data / "build.log"
                    env = {**os.environ, "CARGO_BUILD_JOBS": "2"}
                    with log_path.open("w") as log:
                        result = subprocess.run(
                            ["cargo", "build", "--locked", "--bin", "clinic-voice"],
                            cwd=self.root,
                            env=env,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            timeout=300,
                            check=False,
                        )
                    job["log"] = log_path.read_text()[-20000:]
                    if result.returncode:
                        raise ValueError("Компилятор отклонил сборку")
                    if self.read()["revision"] != expected:
                        raise Conflict(
                            "Файлы изменились во время сборки; версия не опубликована"
                        )
                    artifact = self.data / "artifacts" / job["id"]
                    artifact.mkdir(parents=True)
                    shutil.copy2(
                        self.root / "target/debug/clinic-voice",
                        artifact / "clinic-voice",
                    )
                    (artifact / "project.json").write_text(
                        json.dumps(self.read(), ensure_ascii=False, indent=2)
                    )
                    job.update(
                        status="succeeded",
                        artifact=job["id"],
                        message="Версия собрана. Активный помощник продолжает работать; установка версии выполняется при обновлении сервиса.",
                    )
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                job.update(status="failed", error=str(exc))
            finally:
                self.build_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return dict(self.job)


def make_server(repo, static, host="127.0.0.1", port=8090, token=""):
    from .editor_auth import EditorAuth

    auth = EditorAuth(
        os.environ.get(
            "EDITOR_PASSWORD_FILE", str(repo.root / "secrets/admin-password.json")
        ),
        os.environ.get("EDITOR_API_TOKEN", ""),
    )
    from .demo_call import DemoCalls
    from .sandbox_store import SandboxStore
    from .module_settings import ModuleSettings
    settings = ModuleSettings(repo.data / 'modules.json')
    from .specialty_dictionary import SpecialtyDictionary
    import shutil
    dictionary_path=repo.data/'specialties.json'
    if not dictionary_path.exists():shutil.copyfile(Path(__file__).with_name('specialty_names.json'),dictionary_path)
    dictionary=SpecialtyDictionary(dictionary_path)

    demo = DemoCalls(store=SandboxStore(repo.data / 'call-log'),settings=settings)
    demo.dictionary=dictionary
    from .data_sources import DataSources
    source_roots=[repo.root/'reference-data',repo.root/'imports',repo.root.parent,Path('/imports'),Path('/source-files')]
    data_sources=DataSources(repo.data/'data-sources',demo.store,source_roots,flat_roots=[repo.root.parent,Path('/source-files')])
    from .workbench import Connections,Workbench
    workbench=Workbench(demo.store,Connections(repo.data/'connections.json'))
    from .call_analysis import Uploads
    demo.uploads=Uploads(demo)
    static = Path(static).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.handle_request(False)

        def do_POST(self):
            self.handle_request(True)

        def handle_request(self, post):
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                return self.send_json(200, {"status": "ok"})
            if parsed.path.startswith("/api/"):
                identity = auth.identity(self.headers)
                workshop = parsed.path.startswith('/api/workshop/')
                demo_call = parsed.path.startswith('/api/demo-call/')
                if identity is None and (workshop or demo_call or parsed.path == '/api/me'):
                    identity = {'username':'observer','role':'observer','registrar':None,'label':'Наблюдатель'}
                if parsed.path != '/api/login' and identity is None:
                    return self.send_json(401, {'error':'Войдите с паролем администратора'})
                manual_read=parsed.path in {'/api/workshop/sql','/api/workshop/request'}
                if identity and identity['role'] == 'observer' and post and parsed.path != '/api/login' and not demo_call and not manual_read:
                    return self.send_json(403, {'error':'Наблюдатель может только просматривать'})
                extensions = parsed.path.startswith('/api/extensions/')
                if identity and identity['role'] == 'registrar' and not (workshop or demo_call or extensions or parsed.path in {'/api/me','/api/logout'}):
                    return self.send_json(403, {'error':'Конструктор всей системы доступен администратору'})
                origin = self.headers.get("Origin")
                if (
                    post
                    and origin
                    and urlsplit(origin).netloc != self.headers.get("Host")
                ):
                    return self.send_json(
                        403, {"error": "Запрос с другого сайта отклонён"}
                    )
                try:
                    data = {}
                    if post:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= LIMIT:
                            return self.send_json(
                                413, {"error": "Размер проекта превышает 3 MiB"}
                            )
                        data = json.loads(self.rfile.read(length))
                        if not isinstance(data, dict):
                            raise ValueError("Ожидается объект проекта")
                    route = parsed.path
                    if route=='/api/settings/connections':
                        if identity['role']!='admin':return self.send_json(403,{'error':'Требуется администратор'})
                        result=workbench.connections.save(data.get('connections'),data.get('revision')) if post else workbench.connections.read()
                        return self.send_json(200,result)
                    if route=='/api/workshop/connections' and not post:return self.send_json(200,workbench.public())
                    if route=='/api/workshop/reference-mis' and not post:
                        return self.send_json(200,{'service':'reference_mis','schema':'/api/workshop/knowledge/document?id=mis%3Acontract','operations':['GET /doctors','GET /clients','GET /visits'],'console':'/registrar','note':'Выполнение через панель Ручные API; тестовые данные'})
                    if route=='/api/workshop/sql' and not post:
                        return self.send_json(200,{'service':'reference_sqlite','tables':sorted(workbench.sql.TABLES),'console':'/registrar','request':{'sql':'SELECT id, full_name FROM patients LIMIT 20','params':[]},'note':'POST выполняет запрос в выбранном подключении; референсные данные входят в проект'})
                    if route in {'/api/workshop/sql','/api/workshop/request'} and post:
                        return self.send_json(200,workbench.request(route,data,identity))
                    if route in {'/api/settings/sources','/api/settings/sources/check','/api/settings/sources/apply','/api/settings/scenarios','/api/settings/specialties','/api/settings/plans','/api/settings/modules','/api/settings/models','/api/sandbox/tables','/api/calls','/api/calls/detail','/api/calls/import','/api/calls/transcribe','/api/calls/audio','/api/calls/upload'}:
                        if identity.get('role') != 'admin':
                            return self.send_json(403,{'error':'Требуется администратор'})
                        query=parse_qs(parsed.query)
                        if route=='/api/settings/sources':
                            result=data_sources.save(data.get('sources'),data.get('revision')) if post else data_sources.read()
                        elif route=='/api/settings/sources/check' and post:
                            result=data_sources.inspect(data.get('id'),data.get('revision'))
                        elif route=='/api/settings/sources/apply' and post:
                            result=data_sources.apply(data.get('revision'))
                        elif route=='/api/settings/scenarios' and not post:
                            from .scenario_catalog import catalog
                            result=catalog()
                        elif route=='/api/settings/specialties':
                            result=dictionary.save(data.get('specialties'),data.get('revision')) if post else dictionary.read()
                        elif route=='/api/settings/plans' and not post:
                            from .dialogue_plan import plans
                            result=plans(dictionary)
                        elif route=='/api/settings/modules':
                            result=settings.save(data.get('modules')) if post else settings.public()
                        elif route=='/api/settings/models' and not post:
                            from .module_settings import model_catalog
                            result=model_catalog(query.get('module',[''])[0])
                        elif route=='/api/sandbox/tables' and not post:result=demo.store.tables()
                        elif route=='/api/calls' and not post:result={'calls':demo.store.list_calls()}
                        elif route=='/api/calls/detail' and not post:result=demo.store.call(query.get('id',[''])[0])
                        elif route=='/api/calls/audio' and not post:
                            key=query.get('id',[''])[0];filename=query.get('file',[''])[0]
                            doc=demo.store.call(key)
                            folder=(demo.store.audio/key).resolve();path=(folder/filename).resolve()
                            if not doc.get('consent_audio') or path.parent!=folder or path.suffix!='.wav' or not path.is_file():
                                return self.send_json(404,{'error':'Аудиозапись не найдена'})
                            body=path.read_bytes();self.send_response(200);self.send_header('Content-Type','audio/wav');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();return self.wfile.write(body)
                        elif route=='/api/calls/import' and post:
                            from .call_analysis import analyze
                            result=analyze(demo,data,identity['username'])
                        elif route=='/api/calls/upload' and post:
                            if not settings.read()['asr']['enabled']:raise ValueError('Распознавание речи отключено')
                            result=demo.uploads.request(data,identity['username'])
                        elif route=='/api/calls/transcribe' and post:
                            if not settings.read()['asr']['enabled']:raise ValueError('Распознавание речи отключено')
                            from .call_analysis import transcribe
                            result=transcribe(demo,data.get('wav_b64'))
                        else:raise ValueError('Неизвестное действие')
                        return self.send_json(200,result)
                    if extensions:
                        from .scenario_extensions import request as extension_request
                        op=route.rsplit('/',1)[-1]
                        if (post and op not in {'add','compile','remove'}) or (not post and op!='list'):raise ValueError('Неизвестное действие расширения')
                        params=data if post else {k:v[0] for k,v in parse_qs(parsed.query).items()}
                        return self.send_json(200,extension_request(demo.store,settings,identity,op,params))
                    if route.startswith('/api/insights/'):
                        operations = {
                            (False, '/api/insights/dashboard'): 'insights.dashboard',
                            (True, '/api/insights/complaint/resolve'): 'complaint.resolve',
                            (True, '/api/insights/profile/review'): 'profile.review',
                        }
                        operation = operations.get((post, route))
                        if operation is None:
                            return self.send_json(404, {'error': 'Маршрут не найден'})
                        if identity.get('role') != 'admin':
                            return self.send_json(403, {'error': 'Требуется администратор'})
                        from .admin_rpc import module_call
                        from .insights_access import sign_request
                        arguments = data if post else {}
                        query = parse_qs(parsed.query)
                        if not post and query.get('registrar'):
                            arguments = {'registrar': query['registrar'][0]}
                        envelope = sign_request(operation, arguments, identity)
                        return self.send_json(200, module_call('crm', 'control.insights', envelope))
                    if demo_call:
                        data['_line']=identity.get('registrar') or str(data.get('registrar') or '100')
                        return self.send_json(200, demo.request(route, post, data, parse_qs(parsed.query), client=self.client_address[0]))
                    if post and route == "/api/login":
                        session = auth.login(
                            data.get("password", ""), self.client_address[0], data.get("username", "admin")
                        )
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header(
                            "Set-Cookie",
                            "clinic_admin="
                            + session
                            + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"
                            + (
                                "; Secure"
                                if os.environ.get("EDITOR_SECURE_COOKIE") == "1"
                                else ""
                            ),
                        )
                        self.end_headers()
                        self.wfile.write(b'{"authenticated":true}')
                        return
                    if post and route == "/api/logout":
                        auth.logout(self.headers)
                        return self.send_json(200, {"authenticated": False})
                    if not post and route == '/api/me':
                        return self.send_json(200, identity)
                    if not post and route == '/api/accounts':
                        return self.send_json(200, {'accounts':[{'username':key,'registrar':value['registrar'],'label':value.get('label','')} for key,value in auth.accounts().items()]})
                    if post and route == '/api/accounts':
                        return self.send_json(200, auth.add_account(data['username'],data['registrar'],data['password'],data.get('label','')))
                    if not post and route == '/api/workshop/voice':
                        from .workshop import recorded_voice
                        query=parse_qs(parsed.query)
                        raw=recorded_voice(repo,query.get('state',[''])[0],int(query.get('index',['0'])[0]))
                        self.send_response(200)
                        self.send_header('Content-Type','audio/wav')
                        self.send_header('Content-Length',str(len(raw)))
                        self.send_header('Cache-Control','no-store')
                        self.end_headers();self.wfile.write(raw)
                        return
                    if route.startswith('/api/workshop/'):
                        from .workshop import request
                        return self.send_json(200, request(repo,route,post,data,parse_qs(parsed.query),{**identity,'_client':self.client_address[0]},store=demo.store))
                    if not post and route.startswith('/api/audio/file/'):
                        from .admin_audio import recording
                        raw = recording(repo.root, route.rsplit('/', 1)[1])
                        self.send_response(200)
                        self.send_header('Content-Type', 'audio/wav')
                        self.send_header('Content-Length', str(len(raw)))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                    if post and route == '/api/audio/preview':
                        from .admin_audio import preview
                        return self.send_json(200,preview(data))
                    if not post and route == '/api/audio':
                        from .admin_audio import listing
                        return self.send_json(200, listing(repo.root))
                    if post and route in {'/api/audio/refresh', '/api/audio/replace', '/api/audio/audit'}:
                        from .admin_rpc import module_call
                        op = 'bank.refresh' if route.endswith('/refresh') else 'bank.audit' if route.endswith('/audit') else 'bank.replace'
                        return self.send_json(200, module_call('tts', op, data))
                    if not post and route == "/api/project":
                        result = repo.read()
                    elif not post and route == "/api/search":
                        result = {
                            "results": repo.search(
                                parse_qs(parsed.query).get("q", [""])[0]
                            )
                        }
                    elif not post and route == "/api/history":
                        result = {"versions": repo.history()}
                    elif not post and route == "/api/build":
                        result = repo.build_status()
                    elif post and route == "/api/validate":
                        result = repo.inspect(data["project"])
                    elif post and route == "/api/save":
                        result = repo.save(data["project"], data.get("revision"))
                    elif post and route == "/api/restore":
                        result = repo.restore(data["version"], data.get("revision"))
                    elif post and route == "/api/import":
                        source = str(data["text"])
                        if len(source) > LIMIT:
                            raise ValueError("Слишком большой сценарий")
                        result = clean_flow(source)
                    elif post and route == "/api/build":
                        result = repo.start_build(data.get("revision"))
                    else:
                        return self.send_json(404, {"error": "Маршрут не найден"})
                    return self.send_json(200, result)
                except Conflict as exc:
                    return self.send_json(409, {"error": str(exc)})
                except (ValueError, KeyError, TypeError, FlowError) as exc:
                    return self.send_json(400, {"error": str(exc)})
                except (OSError, sqlite3.Error):
                    return self.send_json(
                        500,
                        {
                            "error": "Не удалось сохранить проект. Проверьте журнал редактора."
                        },
                    )
            if post:
                return self.send_json(404, {"error": "Маршрут не найден"})
            relative = "index.html" if parsed.path in {"/", "/registrar"} else parsed.path.lstrip("/")
            path = (static / relative).resolve()
            if not path.is_relative_to(static) or not path.is_file():
                return self.send_json(404, {"error": "Файл не найден"})
            body = path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(path)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument(
        "--data", default=os.environ.get("EDITOR_DATA", str(ROOT / "debug/runs/editor"))
    )
    parser.add_argument("--static", default=str(ROOT / "scenario-editor/dist"))
    parser.add_argument("--host", default=os.environ.get("EDITOR_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("EDITOR_PORT", "8090"))
    )
    parser.add_argument(
        "--allow-build",
        action="store_true",
        default=os.environ.get("EDITOR_ALLOW_BUILD") == "1",
    )
    args = parser.parse_args()
    repo = Repository(args.root, args.data, args.allow_build)
    server = make_server(repo, args.static, args.host, args.port)
    print(f"Scenario editor: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

"""Isolated real editor + real Learning SQLite for browser Actions acceptance.

The optional archive JSON may come from another runner. The default archive is
synthetic and is never evidence that an actual FSM or speech recognizer ran.
"""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import threading

from calls.crm.registry import MockRegistry
from clinic.call_archive import project_graph
from clinic.learning import Learning
from clinic.scenario_editor import Repository, make_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    at = datetime(2026, 10, 22, 20, tzinfo=timezone.utc)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with tempfile.TemporaryDirectory(prefix='clinic-actions-editor-') as temp:
        isolated = Path(temp)
        shutil.copytree(args.root / 'scenarios', isolated / 'scenarios')
        os.environ['EDITOR_PASSWORD_FILE'] = str(isolated / 'password.json')
        os.environ['EDITOR_BOOTSTRAP_PASSWORD'] = 'admin'
        repo = Repository(isolated, isolated / 'data')
        reg = MockRegistry(isolated / 'learning.sqlite')
        learning = Learning(reg)
        project = repo.read()
        archive = json.loads(args.archive.read_text()) if args.archive else {
            'call_id': 'synthetic-runtime-action-probe', 'registrar': '100',
            'finished': True, 'outcome': 'synthetic_archive_only', 'duration_s': 18,
            'trace': ['inbound.welcome', 'inbound.listen_free', 'inbound.operator'],
            'transcript': [
                {'role': 'assistant', 'text': 'Здравствуйте. Это синтетическая архивная реплика.', 'state': 'inbound.welcome', 'step_index': 0, 'source': 'synthetic_archive'},
                {'role': 'patient', 'text': 'Перенесите мой приём на утро, пожалуйста.', 'state': 'inbound.listen_free', 'step_index': 1, 'source': 'synthetic_archive'},
                {'role': 'assistant', 'text': 'Передаю вопрос регистратору.', 'state': 'inbound.operator', 'step_index': 2, 'source': 'synthetic_archive'},
            ],
        }
        expected_target = archive.get('expected_target') if args.archive else 'inbound.operator'
        if not expected_target:
            raise ValueError('Provided archive must name an independent expected_target')
        archive = {**archive, 'registrar': '100'}
        if not archive.get('trace') or not archive.get('transcript'):
            raise ValueError('Archive must contain a recorded transcript and trace')
        archive.setdefault('call_id', 'provided-runtime-action-probe')
        if not archive.get('scenario_revision'):
            archive.update(scenario_revision=project['revision'], graph=project_graph(project))
        status, payload = learning.handle('learning.archive', archive, at)
        if status != 'ok':
            raise ValueError(payload)
        source_state = next((t['state'] for t in archive['transcript'] if t.get('role') == 'patient'), archive['trace'][0])
        operations = []
        state_lock = threading.RLock()

        def snapshot():
            with state_lock:
                facts = {
                    'archive_origin': 'provided_json_archive' if args.archive else 'synthetic_archive_fixture',
                    'actual_fsm_executed_by_this_runner': False,
                    'current_project_revision': repo.read()['revision'],
                    'cases': reg.execute('SELECT * FROM learning_cases ORDER BY id'),
                    'labels': reg.execute('SELECT * FROM learning_labels ORDER BY id'),
                    'booking_count': reg.execute('SELECT COUNT(*) AS n FROM bookings')[0]['n'],
                    'operations': list(operations),
                }
                target = args.out / 'sqlite-effects.json'
                temporary = target.with_suffix('.tmp')
                temporary.write_text(json.dumps(facts, ensure_ascii=False, indent=2) + '\n')
                temporary.replace(target)

        class LearningHttp(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                operation = self.path.removeprefix('/v1/')
                if operation not in {'learning.calls', 'learning.call', 'learning.propose', 'learning.cases', 'learning.night', 'learning.review'}:
                    self.send_error(403, 'Only learning operations are permitted by this test adapter')
                    return
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size < 1024 * 1024:
                    self.send_error(413)
                    return
                body = json.loads(self.rfile.read(size))
                status, payload = learning.handle(operation, body, at, closed=True)
                with state_lock:
                    operations.append({'operation': operation, 'status': status, 'registrar': body.get('registrar')})
                    snapshot()
                raw = json.dumps({'status': status, 'payload': payload}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        crm = ThreadingHTTPServer(('127.0.0.1', 0), LearningHttp)
        os.environ['CRM_URL'] = f'http://127.0.0.1:{crm.server_port}'
        editor = make_server(repo, args.root / 'scenario-editor/dist', host='127.0.0.1', port=0)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (crm, editor)]
        for thread in threads:
            thread.start()
        snapshot()
        ready = {
            'base': f'http://127.0.0.1:{editor.server_port}', 'crm': os.environ['CRM_URL'],
            'archive_origin': 'provided_json_archive' if args.archive else 'synthetic_archive_fixture',
            'call_id': archive['call_id'], 'trace': archive['trace'], 'transcript': archive['transcript'],
            'source_state': source_state, 'target_state': expected_target,
        }
        (args.out / 'ready.json').write_text(json.dumps(ready, ensure_ascii=False, indent=2) + '\n')
        try:
            stop.wait(240)
        finally:
            for server in (editor, crm):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)
            snapshot()
            repo.db.close()
            reg._conn.close()


if __name__ == '__main__':
    main()

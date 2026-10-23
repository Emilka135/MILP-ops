"""Read the shared scenario pack without CRM, model calls or patient context."""
import hashlib
import json
from pathlib import Path


class ScenarioKnowledge:
    def __init__(self, root, project):
        self.root = Path(root).resolve()
        self.folder = (self.root / 'scenarios').resolve()
        self.project = project

    def _documents(self):
        result = {}

        def add(identifier, path, title, format='text'):
            resolved = path.resolve()
            if not resolved.is_relative_to(self.folder) or not resolved.is_file():
                return
            if resolved.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('Документ сценария превышает 2 MiB')
            content = resolved.read_text(encoding='utf-8')
            result[identifier] = {'id': identifier, 'title': title,
                                  'path': str(path.relative_to(self.root)), 'format': format,
                                  'content': content,
                                  'revision': hashlib.sha256(content.encode()).hexdigest()}

        add('guide', self.folder/'README.md', 'Как работать со сценариями', 'markdown')
        for flow in self.project['flows']:
            name = flow['name']
            add('flow:'+name, self.folder/(name+'.flow'), flow.get('label') or name)
        for path in sorted((self.folder/'docs').glob('*.md')):
            add('docs:'+path.stem.lower(), path, path.stem.replace('_', ' '), 'markdown')
        for path in sorted((self.folder/'skills').glob('*.md')):
            add('skills:'+path.stem.lower(), path,
                'Инструкция сотруднику' if path.stem == 'people' else 'Инструкция для ИИ', 'markdown')
        add('reference:regulation', self.folder/'reference/reglament.txt', 'Исходный регламент')
        add('reference:cases', self.folder/'reference/docx_index.json', 'Индекс исходных случаев')
        add('mis:contract', self.folder/'mis/openapi.json', 'Контракт API МИС')
        return result

    def index(self):
        documents = self._documents()
        versions = sorted((key, value['revision']) for key, value in documents.items())
        revision = hashlib.sha256(json.dumps(versions).encode()).hexdigest()
        operations = []
        contract = documents.get('mis:contract')
        if contract:
            for path, methods in json.loads(contract['content'])['paths'].items():
                for method, operation in methods.items():
                    if isinstance(operation, dict) and operation.get('operationId'):
                        operations.append({'operation_id': operation['operationId'],
                                           'method': method.upper(), 'path': path,
                                           'title': operation.get('summary', operation['operationId'])})
        return {'revision': revision, 'scenario_revision': self.project['revision'],
                'flows': [{'name': f['name'], 'title': f.get('label') or f['name'],
                           'entry': f['entry'], 'state_count': len(f['states']),
                           'source_document': 'flow:'+f['name']} for f in self.project['flows']],
                'documents': [{k: v for k, v in d.items() if k != 'content'} for d in documents.values()],
                'operations': operations, 'source': 'local_scenario_pack',
                'referrals_source': 'optional_local_clinic_registry',
                'manual_requests': 'draft_only_no_network'}

    def document(self, identifier):
        # IDs come exclusively from the allowlist, never from a browser path.
        result = self._documents().get(identifier)
        if result is None:
            raise ValueError('Документ сценария не найден')
        return result

    def flow(self, name):
        flow = next((f for f in self.project['flows'] if f['name'] == name), None)
        if flow is None:
            raise ValueError('Сценарий не найден')
        return {'revision': self.project['revision'], 'flow': flow,
                'source_document': 'flow:'+name, 'read_only': True}

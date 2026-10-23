"""Bind executed contract reports to one source tree and one invocation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

MODULES = ('source', 'label', 'entities', 'fsm', 'mis', 'audio')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(root, run_id=None):
    root = Path(root)
    run_id = run_id or os.environ.get('CLINIC_CONTRACT_RUN_ID')
    if not run_id:
        raise ValueError('Set CLINIC_CONTRACT_RUN_ID or --run-id for this invocation')
    paths = []
    for name in ('src', 'scenarios', 'runtime/src', 'tests/contracts', 'data/acceptance'):
        paths.extend(p for p in (root / name).rglob('*')
                     if p.is_file() and p.suffix in {'.py', '.rs', '.flow', '.json'}
                     and '__pycache__' not in p.parts)
    paths.extend(root / 'debug' / name for name in
                 ('contract_evidence.py', 'run_docx_contracts.py', 'check_docx_coverage.py'))
    content = [(str(p.relative_to(root)), sha256(p)) for p in sorted(set(paths)) if p.exists()]
    fingerprint = hashlib.sha256(json.dumps(content, separators=(',', ':')).encode()).hexdigest()
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                              text=True, capture_output=True, timeout=5).stdout.strip()
    return {'schema': 1, 'run_id': run_id, 'git_sha': revision, 'source_sha256': fingerprint}


def validate_report(folder, module, expected, now=None, max_age=7200):
    """Reject stale/mixed/truncated reports before looking at testcase properties."""
    folder = Path(folder)
    report = json.loads((folder / 'results.json').read_text())
    observed = report.get('provenance', {})
    for key, value in expected.items():
        if observed.get(key) != value:
            raise ValueError(f'{module}: evidence {key} does not match current invocation')
    if report.get('module') != module or report.get('pytest_exit') != 0 or report.get('passed') is not True:
        raise ValueError(f'{module}: execution did not pass')
    now = time.time() if now is None else now
    start, end = observed.get('started_at', 0), observed.get('finished_at', 0)
    if not (0 < start <= end <= now + 30) or now - end > max_age:
        raise ValueError(f'{module}: stale or invalid execution time')
    if observed.get('xml_sha256') != sha256(folder / 'results.xml'):
        raise ValueError(f'{module}: JUnit content differs from executed report')
    return report

"""A green old XML must not masquerade as current scenario acceptance."""
import json
from pathlib import Path
import time

import pytest

from debug.contract_evidence import identity, sha256, validate_report

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def evidence(tmp_path):
    expected = identity(ROOT, 'unit-run')
    (tmp_path/'results.xml').write_text('<testsuite><testcase name="executed"/></testsuite>')
    provenance = dict(expected, started_at=time.time()-2, finished_at=time.time()-1,
                      xml_sha256=sha256(tmp_path/'results.xml'))
    report = dict(module='fsm', passed=True, pytest_exit=0, provenance=provenance)
    (tmp_path/'results.json').write_text(json.dumps(report))
    return tmp_path, expected, report


def save(folder, report):
    (folder/'results.json').write_text(json.dumps(report))


def test_accepts_executed_current_invocation(evidence):
    folder, expected, _ = evidence
    assert validate_report(folder, 'fsm', expected)['passed']


@pytest.mark.parametrize('field', ['run_id', 'git_sha', 'source_sha256', 'schema'])
def test_rejects_other_run_commit_or_scenario_tree(evidence, field):
    folder, expected, report = evidence
    report['provenance'][field] = 'other'
    save(folder, report)
    with pytest.raises(ValueError, match=field):
        validate_report(folder, 'fsm', expected)


@pytest.mark.parametrize('change', ['old', 'future', 'reverse', 'failed', 'wrong_module', 'changed_xml'])
def test_rejects_stale_failed_or_replaced_report(evidence, change):
    folder, expected, report = evidence
    if change == 'old':report['provenance'].update(started_at=1, finished_at=2)
    if change == 'future':report['provenance'].update(started_at=time.time()+200, finished_at=time.time()+201)
    if change == 'reverse':report['provenance']['started_at']=time.time()+1
    if change == 'failed':report.update(passed=False, pytest_exit=1)
    if change == 'wrong_module':report['module']='label'
    if change == 'changed_xml':(folder/'results.xml').write_text('<testsuite/>')
    save(folder, report)
    with pytest.raises(ValueError):validate_report(folder, 'fsm', expected)


def test_missing_report_is_not_accepted(evidence):
    folder, expected, _ = evidence
    (folder/'results.json').unlink()
    with pytest.raises(OSError):validate_report(folder, 'fsm', expected)


def test_run_id_required(monkeypatch):
    monkeypatch.delenv('CLINIC_CONTRACT_RUN_ID', raising=False)
    with pytest.raises(ValueError, match='run-id'):identity(ROOT)


def test_remote_write_socket_is_blocked_before_connection():
    import socket
    # The guard must reject before touching an OS socket, including in a sandbox
    # where creating a socket itself is forbidden.
    with pytest.raises(AssertionError, match='CONTRACT_EXTERNAL_NETWORK_BLOCKED'):
        socket.socket.connect(None, ('203.0.113.1', 443))

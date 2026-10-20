"""Probe boundaries must preserve the compiled engine and correlation checks."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get('CONTRACT_REPLAY_BIN', ROOT / 'target/debug/contract-replay'))


def replay(events=(), **settings):
    run = subprocess.run([str(BINARY)], input=json.dumps({'events': list(events), **settings}),
                         text=True, capture_output=True, timeout=20)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_replay_exercises_current_compiled_scenario():
    result = replay()
    assert len(result['scenario_revision']) == 64
    assert result['frames'][0]['state'].startswith('inbound.')
    assert any(action['kind'] == 'say' for action in result['frames'][0]['actions'])


def test_uncorrelated_module_reply_cannot_inject_patient():
    result = replay([{'kind': 'module', 'value': 'ok', 'request_id': 999999,
                      'payload': {'patient_id': 98765, 'booking_id': 65432}}])
    assert result['frames'][-1]['vars'] == result['frames'][0]['vars']
    assert result['frames'][-1]['state'] == result['frames'][0]['state']


@pytest.mark.parametrize('payload', ['{', '{"flow":"unknown","events":[]}', '{"events":42}'])
def test_invalid_contract_inputs_fail_without_success_json(payload):
    run = subprocess.run([str(BINARY)], input=payload, text=True, capture_output=True, timeout=20)
    assert run.returncode != 0 and not run.stdout
    assert json.loads(run.stderr)['error']

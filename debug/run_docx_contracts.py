"""Run source contracts, retaining one result per C/D/module in CI and debug."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from contract_evidence import identity, sha256

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--module', choices=['source', 'label', 'entities', 'fsm', 'mis', 'audio'], required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'debug/runs/docx-contracts')
    parser.add_argument('--run-id', help='Shared invocation ID; defaults to CLINIC_CONTRACT_RUN_ID')
    parser.add_argument('--timeout', type=int, default=300, help='Maximum pytest seconds per module')
    args = parser.parse_args()
    provenance = identity(ROOT, args.run_id)
    if args.timeout < 1:
        parser.error('--timeout must be positive')
    folder = args.output.resolve()/args.module
    folder.mkdir(parents=True, exist_ok=True)
    junit = folder/'results.xml'
    junit.unlink(missing_ok=True)
    command = [sys.executable, '-m', 'pytest', ('tests/contracts/test_source_inventory.py' if args.module=='source' else 'tests/contracts/test_docx_cases.py'), '-vv', '--tb=short',
               '-k', 'test_'+args.module, '--junitxml='+str(junit), '--basetemp='+str(folder/'pytest-temp')]
    env = dict(os.environ, PYTHONPATH=str(ROOT/'runtime/src')+os.pathsep+str(ROOT),
               CLINIC_CONTRACT_RUN_ID=provenance['run_id'], CLINIC_ACCEPTANCE_TEST='1')
    # A runner launched from a credentialed developer shell must remain isolated.
    for key in ('OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'DBI_API_TOKEN', 'STAND_TOKEN',
                'SMS_GATEWAY_TOKEN', 'STAND_URL', 'OPENAI_BASE_URL', 'SMS_GATEWAY_URL'):
        env[key] = ''
    started_at = time.time()
    timed_out = False
    with (folder/'pytest.log').open('w') as stream:
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, check=False, timeout=args.timeout)
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            timed_out, exit_code = True, 124
            stream.write('\nCONTRACT_EXECUTION_TIMEOUT\n')
    checks = []
    if junit.exists():
        for case in ET.parse(junit).iter('testcase'):
            failure = case.find('failure')
            error = case.find('error')
            skipped = case.find('skipped')
            detail = failure if failure is not None else error
            code = re.search(r'\[([^\]]+)\]$', case.get('name', ''))
            check = {'id': code[1] if code else case.get('name'), 'module': args.module,
                     'status': 'failed' if detail is not None else 'skipped' if skipped is not None else 'passed',
                     'seconds': float(case.get('time', 0))}
            if detail is not None:
                check['failure'] = (detail.text or detail.get('message', ''))[-12000:]
            checks.append(check)
    failed = exit_code != 0 or not checks or any(c['status'] != 'passed' for c in checks)
    provenance.update(started_at=started_at, finished_at=time.time(),
                      xml_sha256=sha256(junit) if junit.exists() else None)
    report = {'module': args.module, 'checks': checks, 'passed': not failed, 'pytest_exit': exit_code,
              'timed_out': timed_out, 'provenance': provenance,
              'external_llm_calls': 0, 'mis_mode': 'isolated_contract', 'voice_gate': 'independent_real_audio'}
    (folder/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    lines = [f"{c['id']}: {c['status'].upper()}" for c in checks]
    print('\n'.join(lines), flush=True)
    if not checks:
        print('CONTRACT_COLLECTION_FAILED: inspect '+str(folder/'pytest.log'))
    if os.environ.get('GITHUB_ACTIONS'):
        for check in checks:
            if check['status'] != 'passed':
                safe_id = re.sub(r'[^A-Za-z0-9_.-]', '_', str(check['id']))
                print(f'::error title={safe_id}::{safe_id} failed; see docx-contract-{args.module} artifact')
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a') as stream:
                stream.write(f'### DOCX / {args.module}\n\n'+ '\n'.join('- `'+line+'`' for line in lines)+'\n')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())

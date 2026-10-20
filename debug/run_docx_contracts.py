"""Run source contracts, retaining one result per C/D/module in CI and debug."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--module', choices=['source', 'label', 'entities', 'fsm', 'mis', 'audio'], required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'debug/runs/docx-contracts')
    args = parser.parse_args()
    folder = args.output.resolve()/args.module
    folder.mkdir(parents=True, exist_ok=True)
    junit = folder/'results.xml'
    command = [sys.executable, '-m', 'pytest', ('tests/contracts/test_source_inventory.py' if args.module=='source' else 'tests/contracts/test_docx_cases.py'), '-vv', '--tb=short',
               '-k', 'test_'+args.module, '--junitxml='+str(junit), '--basetemp='+str(folder/'pytest-temp')]
    with (folder/'pytest.log').open('w') as stream:
        result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False)
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
    failed = result.returncode != 0 or not checks or any(c['status'] != 'passed' for c in checks)
    report = {'module': args.module, 'checks': checks, 'passed': not failed, 'pytest_exit': result.returncode,
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

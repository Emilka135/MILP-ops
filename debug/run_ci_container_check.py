"""Start the real Compose stack and retain actionable startup/audio evidence on failure.

Public CI metadata identifies the failed startup step, not its underlying cause.
This verifier preserves the evidence needed to diagnose the next published run.
All orchestrator dependencies, including the mandatory audio release gate, run.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('debug/runs/containers/startup'))
    parser.add_argument('--compose-file', action='append', default=[])
    parser.add_argument('--project')
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--expected-provider', default='CPUExecutionProvider')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose']
    if args.project:
        compose += ['--project-name', args.project]
    for filename in args.compose_file:
        compose += ['--file', filename]

    def capture(command, filename):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        (args.output / filename).write_text(result.stdout)
        return result

    capture(['docker', 'version'], 'docker-version.txt')
    capture(compose + ['version'], 'compose-version.txt')
    started = time.monotonic()
    startup = capture(compose + ['up', '--detach', '--wait', '--wait-timeout', '240',
                                 'orchestrator', 'scenario-editor'], 'compose-up.txt')
    startup_seconds = round(time.monotonic() - started, 3)
    print(startup.stdout, end='', flush=True)
    capture(compose + ['logs', '--no-color'], 'container-logs.txt')
    capture(compose + ['ps', '--all', '--format', 'json'], 'compose-ps.json')
    ids = capture(compose + ['ps', '--all', '--quiet'], 'container-ids.txt')
    states = []
    if ids.returncode == 0 and ids.stdout.strip():
        inspection = subprocess.run(['docker', 'inspect', *ids.stdout.split()], capture_output=True, text=True, check=False)
        if inspection.returncode == 0:
            # Full inspect includes credentials in Config.Env. Save only startup diagnostics.
            for item in json.loads(inspection.stdout):
                states.append({'service': item['Config']['Labels'].get('com.docker.compose.service'),
                               'image': item['Image'], 'state': item['State'],
                               'restart_count': item['RestartCount'],
                               'memory_limit_bytes': item['HostConfig']['Memory']})
    (args.output / 'container-states.json').write_text(json.dumps(states, ensure_ascii=False, indent=2) + '\n')
    failures = []
    if startup.returncode:
        failures.append(f'Compose startup exited {startup.returncode}')
    required = {'orchestrator', 'scenario-editor', 'tts', 'asr', 'audio-init', 'audio-check'}
    missing = required - {item['service'] for item in states}
    if missing:
        failures.append('Missing required services: ' + ', '.join(sorted(missing)))
    for item in states:
        state, service = item['state'], item['service']
        if service in {'audio-init', 'audio-check'}:
            if state['Status'] != 'exited' or state['ExitCode'] != 0:
                failures.append(f'{service}: {state["Status"]}, exit {state["ExitCode"]}')
        elif state['Status'] != 'running' or state.get('Health', {}).get('Status', 'healthy') != 'healthy':
            failures.append(f'{service}: {state["Status"]}, health {state.get("Health", {}).get("Status")}')
        if state.get('OOMKilled'):
            failures.append(f'{service}: killed by OOM')
    audit = None
    try:
        for filename in ('audit.json', 'current.json'):
            copy = capture(compose + ['cp', f'tts:/audio/{filename}', str(args.output / filename)], f'copy-{filename}.txt')
            if copy.returncode:
                raise ValueError(f'Cannot retain {filename}')
        audit = json.loads((args.output / 'audit.json').read_text())
        current = json.loads((args.output / 'current.json').read_text())
        phrases = audit.get('phrases', [])
        if (audit.get('status') != 'passed' or not phrases
                or audit.get('count') != len(phrases)
                or any(p.get('status') != 'passed' for p in phrases)
                or set(current.get('keys', [])) != {p['key'] for p in phrases}
                or current.get('voice') != audit.get('voice')):
            raise ValueError('The complete current audio bank did not pass real ASR/signals audit')
    except (OSError, ValueError, KeyError) as exc:
        failures.append(str(exc))
    if not failures:
        # A real bank waveform proves which provider handled speech, rather than
        # merely trusting the requested provider or a healthy HTTP endpoint.
        probe = '''import base64,json,pathlib,urllib.request,wave
bank=pathlib.Path('/audio');audit=json.loads((bank/'audit.json').read_text())
with wave.open(str(bank/(audit['phrases'][0]['key']+'.wav')),'rb') as wav:
 pcm=wav.readframes(wav.getnframes());rate=wav.getframerate()
req=urllib.request.Request('http://asr:8080/v1/transcribe',data=json.dumps({'pcm_b64':base64.b64encode(pcm).decode(),'rate':rate}).encode(),headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req,timeout=60) as response:print(response.read().decode())
'''
        result = capture(compose + ['exec', '-T', 'tts', 'python', '-c', probe], 'asr-provider.json')
        try:
            payload = json.loads(result.stdout)
            if result.returncode or payload.get('status') != 'ok' or payload.get('payload', {}).get('provider') != args.expected_provider:
                raise ValueError('Real ASR provider differs from ' + args.expected_provider)
        except ValueError as exc:
            failures.append(str(exc))
        try:
            with urllib.request.urlopen(args.base_url.rstrip('/') + '/readyz', timeout=10) as response:
                body = response.read()
                (args.output / 'readyz.json').write_bytes(body)
                if json.loads(body).get('ready') is not True:
                    raise ValueError('Orchestrator is not ready')
        except (OSError, ValueError) as exc:
            failures.append(f'Readiness request failed: {exc}')
    summary = {'status': 'failed' if failures else 'passed', 'failures': failures,
               'compose_exit_code': startup.returncode, 'startup_seconds': startup_seconds, 'audited_phrases': len(audit.get('phrases', [])) if audit else 0,
               'expected_asr_provider': args.expected_provider}
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return bool(failures)


if __name__ == '__main__':
    sys.exit(main())

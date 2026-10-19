"""Start the real Compose stack and retain actionable startup/audio evidence on failure.

Public CI metadata identifies the failed startup step, not its underlying cause.
This verifier preserves the evidence needed to diagnose the next published run.
All orchestrator dependencies, including the mandatory audio release gate, run.
"""

import argparse
import json
import html
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


SERVICES = frozenset({"orchestrator", "scenario-editor", "tts", "asr", "audio-init", "audio-check",
                      "crm", "nlu", "sms", "operator", "agent", "nightly", "nightly-worker", "scenario-builder"})
STATUSES = frozenset({"created", "running", "paused", "restarting", "removing", "exited", "dead"})
AUDIT_STATUSES = frozenset({"passed", "failed", "running", "needs_review"})
STAGES = frozenset({"setup", "docker-version", "compose-version", "compose-config", "compose-up", "compose-wait", "container-diagnostics",
                   "audio-audit", "asr-provider", "readiness", "complete"})


def enum_value(value, allowed, default):
    return value if isinstance(value, str) and value in allowed else default


def dependency_requirements(configuration, targets=("orchestrator", "scenario-editor")):
    """Return target closure and completed-job dependencies, never environment."""
    services = configuration.get("services") if isinstance(configuration, dict) else None
    if not isinstance(services, dict):
        raise ValueError("invalid_compose_services")
    required, completed = set(), set()
    pending = list(targets)
    while pending:
        service = pending.pop()
        if not isinstance(service, str) or service not in services:
            raise ValueError("missing_compose_dependency")
        if service in required:
            continue
        definition = services[service]
        if not isinstance(definition, dict):
            raise ValueError("invalid_compose_service")
        required.add(service)
        dependencies = definition.get("depends_on", {})
        if isinstance(dependencies, list):
            if any(not isinstance(name, str) for name in dependencies):
                raise ValueError("invalid_compose_dependency_name")
            dependencies = {name: {} for name in dependencies}
        if not isinstance(dependencies, dict):
            raise ValueError("invalid_compose_dependencies")
        for name, policy in dependencies.items():
            if not isinstance(name, str) or not isinstance(policy, dict):
                raise ValueError("invalid_compose_dependency_policy")
            pending.append(name)
            if policy.get("condition") == "service_completed_successfully":
                completed.add(name)
    # The voice release must continue to include its complete audio gate even if
    # someone accidentally removes a dependency edge from the Compose source.
    if not {"orchestrator", "scenario-editor", "tts", "asr", "audio-init", "audio-check"} <= required:
        raise ValueError("mandatory_audio_dependency_missing")
    return required, completed


def lifecycle_state(state):
    """Retain only lifecycle fields; health command output is application data."""
    result = {"Status": state.get("Status"), "ExitCode": state.get("ExitCode"),
              "OOMKilled": state.get("OOMKilled") is True}
    health = state.get("Health")
    if isinstance(health, dict) and "Status" in health:
        result["Health"] = {"Status": health["Status"]}
    return result


def bounded_number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not low <= value <= high or not math.isfinite(value):
        return None
    return round(value, 4)


def safe_exception(error):
    # Exception messages can include endpoints, credentials, or patient text.
    for kind in (FileNotFoundError, PermissionError, TimeoutError, ValueError,
                 TypeError, KeyError, AttributeError, OSError, RuntimeError):
        if isinstance(error, kind):
            return kind.__name__
    return "UnexpectedException"


def public_diagnostics(evidence):
    """A strict allowlist suitable for a public GitHub check annotation."""
    containers = []
    states = evidence.get("states", [])
    states = states if isinstance(states, list) else []
    for item in states[:32]:
        if not isinstance(item, dict):
            continue
        state = item.get("state", {})
        state = state if isinstance(state, dict) else {}
        health = state.get("Health", {})
        health = health if isinstance(health, dict) else {}
        containers.append({
            "service": enum_value(item.get("service"), SERVICES, "unknown-service"),
            "status": enum_value(state.get("Status"), STATUSES, "unknown"),
            "health": enum_value(health.get("Status"), {"healthy", "unhealthy", "starting"}, "none"),
            "exit_code": bounded_number(state.get("ExitCode"), -255, 255),
            "oom_killed": state.get("OOMKilled") is True,
        })
    audit, current = evidence.get("audit"), evidence.get("current")
    audit = audit if isinstance(audit, dict) else {}
    current = current if isinstance(current, dict) else {}
    phrases = audit.get("phrases", [])
    phrases = phrases if isinstance(phrases, list) else []
    failed = []
    for phrase in phrases:
        if not isinstance(phrase, dict) or phrase.get("status") == "passed":
            continue
        reasons = []
        for field in ("numbers_match", "polarity_match", "status_match"):
            if phrase.get(field) is False:
                reasons.append(field)
        wer = bounded_number(phrase.get("wer"), 0, 100)
        rms = bounded_number(phrase.get("rms"), 0, 32768)
        clipping = bounded_number(phrase.get("clipped_fraction"), 0, 1)
        if wer is not None and wer > .15:
            reasons.append("wer_above_0.15")
        if rms is not None and rms <= 100:
            reasons.append("rms_not_above_100")
        if clipping is not None and clipping >= .001:
            reasons.append("clipped_fraction_not_below_0.001")
        # Only known fixed errors are rendered. Unknown error contents stay private.
        known_errors = {"Неверный формат WAV": "invalid_wav_format", "Неверная длительность": "invalid_audio_duration"}
        if phrase.get("error") is not None:
            error = phrase["error"]
            reasons.append(known_errors.get(error, "phrase_processing_exception") if isinstance(error, str) else "phrase_processing_exception")
        key = phrase.get("key")
        failed.append({"key": key[:12] if isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key) else "invalid-key",
                       "reasons": reasons or ["phrase_did_not_pass"], "wer": wer,
                       "rms": rms, "clipped_fraction": clipping})
    phrase_keys = [p.get("key") for p in phrases if isinstance(p, dict)]
    current_keys = current.get("keys", [])
    current_keys = current_keys if isinstance(current_keys, list) else []
    # JSON encoding allows malformed unhashable keys to remain a contract failure,
    # without throwing while attempting to report the original verifier failure.
    keys_match = (all(isinstance(key, str) for key in phrase_keys + current_keys)
                  and set(phrase_keys) == set(current_keys))
    stage = evidence.get("stage")
    result = {
        "stage": enum_value(stage, STAGES, "unknown"),
        "compose_exit_code": bounded_number(evidence.get("compose_exit_code"), -255, 255),
        "initial_phase_exit_code": bounded_number(evidence.get("initial_phase_exit_code"), -255, 255),
        "wait_phase_exit_code": bounded_number(evidence.get("wait_phase_exit_code"), -255, 255),
        "containers": containers,
        "audit": {
            "status": enum_value(audit.get("status"), AUDIT_STATUSES, "missing-or-invalid"),
            "declared_count": bounded_number(audit.get("count"), 0, 10000),
            "phrase_count": len(phrases), "current_key_count": len(current_keys),
            "invalid_phrase_entries": sum(not isinstance(p, dict) for p in phrases),
            "count_matches": audit.get("count") == len(phrases),
            "current_keys_match": keys_match,
            "voice_matches": bool(audit.get("voice")) and current.get("voice") == audit.get("voice"),
            "failed_phrase_count": len(failed), "failed_phrases": failed[:8],
        },
    }
    if evidence.get("expected_provider") is not None:
        allowed_providers = {"CPUExecutionProvider", "CUDAExecutionProvider"}
        result["expected_provider"] = enum_value(evidence.get("expected_provider"), allowed_providers, "unknown-provider")
        result["actual_provider"] = enum_value(evidence.get("actual_provider"), allowed_providers, "unknown-provider")
    if isinstance(evidence.get("required_services"), set):
        result["dependency_service_count"] = len(evidence["required_services"])
        result["completed_job_count"] = len(evidence.get("completed_services", set()))
        result["ignored_container_count"] = evidence.get("ignored_container_count", 0)
    for flag in ("provider_failed", "readiness_failed", "summary_write_failed", "dependency_config_failed"):
        if evidence.get(flag) is True:
            result[flag] = True
    if evidence.get("unexpected_exception"):
        result["unexpected_exception"] = enum_value(evidence["unexpected_exception"], {
            "FileNotFoundError", "PermissionError", "TimeoutError", "ValueError", "TypeError", "KeyError", "AttributeError", "OSError", "RuntimeError"
        }, "UnexpectedException")
    return result


def escape_annotation(message):
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def publish_diagnostics(diagnostics, failed, summary_path=None):
    """Emit public diagnostics without raw logs, env, exception bodies or phrases."""
    data = json.dumps(diagnostics, ensure_ascii=True, separators=(",", ":"))
    if failed:
        print("::error title=Container acceptance failed::" + escape_annotation(data), flush=True)
    destination = summary_path if summary_path is not None else os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        rendered = json.dumps(diagnostics, ensure_ascii=True, indent=2)
        with Path(destination).open("a") as stream:
            stream.write("\n### Container acceptance: " + ("failed" if failed else "passed") + "\n\n")
            stream.write("Sanitized service states and audio gate diagnostics; phrase text and logs are excluded.\n\n<pre>" + html.escape(rendered) + "</pre>\n")


def verify(args, evidence):
    compose = ['docker', 'compose']
    if args.project:
        compose += ['--project-name', args.project]
    for filename in args.compose_file:
        compose += ['--file', filename]

    def capture(command, filename):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        (args.output / filename).write_text(result.stdout)
        return result

    evidence['stage'] = 'docker-version'
    capture(['docker', 'version'], 'docker-version.txt')
    evidence['stage'] = 'compose-version'
    capture(compose + ['version'], 'compose-version.txt')
    evidence['stage'] = 'compose-config'
    try:
        # Resolved Compose JSON contains environment secrets: keep both output
        # streams in memory, and never use capture() or print their contents.
        configuration = subprocess.run(compose + ['config', '--format', 'json'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, check=False)
        if configuration.returncode:
            raise ValueError('compose_configuration_failed')
        required, completed_services = dependency_requirements(json.loads(configuration.stdout))
        del configuration
        evidence['required_services'] = required
        evidence['completed_services'] = completed_services
    except (OSError, ValueError, TypeError):
        evidence['dependency_config_failed'] = True
        raise RuntimeError('compose_dependency_configuration_failed') from None
    evidence['stage'] = 'compose-up'
    started = time.monotonic()
    # First run dependency ordering, including successful completion of the
    # one-shot audio jobs. A broad --wait may reject an expected exited job on
    # some Compose versions (docker/compose#11774, #9273).
    initial = capture(compose + ['up', '--detach', 'orchestrator', 'scenario-editor'],
                      'compose-up.txt')
    initial_seconds = round(time.monotonic() - started, 3)
    evidence['initial_phase_exit_code'] = initial.returncode
    print(initial.stdout, end='', flush=True)
    waited = None
    wait_seconds = None
    if initial.returncode == 0:
        evidence['stage'] = 'compose-wait'
        wait_started = time.monotonic()
        # Dependencies already ran above. Wait only for the two persistent
        # endpoints, then still inspect EVERY service and audit the full bank.
        waited = capture(compose + ['up', '--detach', '--no-deps', '--wait',
                                    '--wait-timeout', '240', 'orchestrator', 'scenario-editor'],
                         'compose-wait.txt')
        wait_seconds = round(time.monotonic() - wait_started, 3)
        print(waited.stdout, end='', flush=True)
    else:
        (args.output / 'compose-wait.txt').write_text('Skipped: initial Compose phase failed.\n')
    startup_code = initial.returncode if initial.returncode else waited.returncode
    startup_seconds = round(time.monotonic() - started, 3)
    evidence['wait_phase_exit_code'] = waited.returncode if waited is not None else None
    evidence['compose_exit_code'] = startup_code
    evidence['stage'] = 'container-diagnostics'
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
                               'image': item['Image'], 'state': lifecycle_state(item['State']),
                               'restart_count': item['RestartCount'],
                               'memory_limit_bytes': item['HostConfig']['Memory']})
    # Optional profiles and containers left from earlier configurations are not
    # dependencies of these targets. Retain their sanitized state separately.
    all_states = states
    states = [item for item in all_states if item['service'] in required]
    evidence['states'] = states
    evidence['ignored_container_count'] = len(all_states) - len(states)
    for filename, records in (('container-states.json', states), ('container-states-all.json', all_states)):
        (args.output / filename).write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n')
    failures = []
    if initial.returncode:
        failures.append(f'Compose dependency startup exited {initial.returncode}')
    if waited is not None and waited.returncode:
        failures.append(f'Compose endpoint wait exited {waited.returncode}')
    missing = required - {item['service'] for item in states}
    if missing:
        failures.append('Missing required services: ' + ', '.join(sorted(missing)))
    for item in states:
        state, service = item['state'], item['service']
        if service in completed_services:
            if state['Status'] != 'exited' or state['ExitCode'] != 0:
                failures.append(f'{service}: {state["Status"]}, exit {state["ExitCode"]}')
        elif state['Status'] != 'running' or state.get('Health', {}).get('Status', 'healthy') != 'healthy':
            failures.append(f'{service}: {state["Status"]}, health {state.get("Health", {}).get("Status")}')
        if state.get('OOMKilled'):
            failures.append(f'{service}: killed by OOM')
    evidence['stage'] = 'audio-audit'
    audit = None
    try:
        for filename in ('audit.json', 'current.json'):
            copy = capture(compose + ['cp', f'tts:/audio/{filename}', str(args.output / filename)], f'copy-{filename}.txt')
            if copy.returncode:
                raise ValueError(f'Cannot retain {filename}')
        audit = json.loads((args.output / 'audit.json').read_text())
        evidence['audit'] = audit
        current = json.loads((args.output / 'current.json').read_text())
        evidence['current'] = current
        phrases = audit.get('phrases', [])
        if (audit.get('status') != 'passed' or not phrases
                or audit.get('count') != len(phrases)
                or any(p.get('status') != 'passed' for p in phrases)
                or set(current.get('keys', [])) != {p['key'] for p in phrases}
                or current.get('voice') != audit.get('voice')):
            raise ValueError('The complete current audio bank did not pass real ASR/signals audit')
    except (OSError, ValueError, KeyError) as exc:
        failures.append('Audio audit gate failed: ' + safe_exception(exc))
    if not failures:
        evidence['stage'] = 'asr-provider'
        evidence['expected_provider'] = args.expected_provider
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
            evidence['actual_provider'] = payload.get('payload', {}).get('provider')
            if result.returncode or payload.get('status') != 'ok' or payload.get('payload', {}).get('provider') != args.expected_provider:
                raise ValueError('Real ASR provider differs from ' + args.expected_provider)
        except ValueError as exc:
            evidence['provider_failed'] = True
            failures.append(str(exc))
        evidence['stage'] = 'readiness'
        try:
            with urllib.request.urlopen(args.base_url.rstrip('/') + '/readyz', timeout=10) as response:
                body = response.read()
                (args.output / 'readyz.json').write_bytes(body)
                if json.loads(body).get('ready') is not True:
                    raise ValueError('Orchestrator is not ready')
        except (OSError, ValueError) as exc:
            evidence['readiness_failed'] = True
            failures.append('Readiness request failed: ' + safe_exception(exc))
    summary = {'status': 'failed' if failures else 'passed', 'failures': failures,
               'compose_exit_code': startup_code, 'startup_seconds': startup_seconds,
               'initial_phase_exit_code': initial.returncode, 'initial_phase_seconds': initial_seconds,
               'wait_phase_exit_code': waited.returncode if waited is not None else None, 'wait_phase_seconds': wait_seconds, 'audited_phrases': len(audit.get('phrases', [])) if audit else 0,
               'expected_asr_provider': args.expected_provider,
               'dependency_service_count': len(required), 'completed_job_count': len(completed_services),
               'ignored_container_count': len(all_states) - len(states)}
    if not failures:
        evidence['stage'] = 'complete'
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('debug/runs/containers/startup'))
    parser.add_argument('--compose-file', action='append', default=[])
    parser.add_argument('--project')
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--expected-provider', default='CPUExecutionProvider')
    args = parser.parse_args(argv)
    evidence = {'stage': 'setup'}
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        summary = verify(args, evidence)
    except Exception as error:  # A failed diagnostic must still produce a public cause.
        evidence['unexpected_exception'] = safe_exception(error)
        summary = {'status': 'failed', 'failures': ['Verifier exception: ' + safe_exception(error)]}
    diagnostics = public_diagnostics(evidence)
    summary['public_diagnostics'] = diagnostics
    try:
        (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    except OSError:
        evidence['summary_write_failed'] = True
        summary['status'] = 'failed'
        diagnostics = public_diagnostics(evidence)
        summary['public_diagnostics'] = diagnostics
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    try:
        publish_diagnostics(diagnostics, summary['status'] == 'failed')
    except OSError:
        # Annotation was already emitted for the original failure. If only the
        # summary write failed, emit an explicit additional failure and fail CI.
        evidence['summary_write_failed'] = True
        summary['status'] = 'failed'
        summary['public_diagnostics'] = public_diagnostics(evidence)
        try:
            (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        except OSError:
            pass  # The public annotation and nonzero exit remain mandatory.
        publish_diagnostics(summary['public_diagnostics'], True, summary_path='')
        return True
    return summary['status'] == 'failed'


if __name__ == '__main__':
    sys.exit(main())

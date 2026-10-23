"""Public CI errors explain gate failures without publishing logs or phrase text."""

import importlib.util
import json
import subprocess
import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_container_diagnostics", ROOT / "debug/run_ci_container_check.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def compose_configuration(extra=None):
    services = {
        "orchestrator": {"depends_on": {"tts": {"condition": "service_healthy"},
                                          "asr": {"condition": "service_healthy"},
                                          "audio-check": {"condition": "service_completed_successfully"}},
                         "environment": {"API_KEY": "PRIVATE_CONFIG_SECRET"}},
        "scenario-editor": {"depends_on": {"orchestrator": {"condition": "service_healthy"}}},
        "tts": {"depends_on": {"audio-init": {"condition": "service_completed_successfully"}}},
        "asr": {}, "audio-init": {}, "audio-check": {},
        "stand": {"profiles": ["optional-contract"]},
    }
    if extra:
        services.update(extra)
    return {"services": services}


def phrase(**extra):
    return {"key": "a" * 64, "status": "needs_review", "wer": .25,
            "numbers_match": False, "polarity_match": True, "status_match": True,
            "rms": 4500, "clipped_fraction": 0, **extra}


def evidence():
    return {"stage": "audio-audit", "compose_exit_code": 1,
            "states": [{"service": "audio-check", "state": {
                "Status": "exited", "ExitCode": 1, "OOMKilled": False,
                "Error": "PRIVATE_DAEMON_ERROR", "Health": {"Status": "unhealthy", "Log": [{"Output": "PRIVATE_HEALTH_LOG"}]}}}],
            "audit": {"status": "needs_review", "count": 1, "voice": "PRIVATE_VOICE_DETAILS", "phrases": [phrase(
                text="PRIVATE_PATIENT_PHRASE", actual_asr="PRIVATE_PATIENT_TRANSCRIPT", error="PRIVATE_PROVIDER_CREDENTIAL") ]},
            "current": {"voice": "PRIVATE_VOICE_DETAILS", "keys": ["a" * 64]}}


def test_public_diagnostics_explain_audio_gate_and_service_exit_without_private_fields(tmp_path, capsys):
    public = ci.public_diagnostics(evidence())
    ci.publish_diagnostics(public, True, summary_path=tmp_path / "summary.md")
    annotation = capsys.readouterr().out
    rendered = (tmp_path / "summary.md").read_text()
    assert "PRIVATE_" not in annotation + rendered
    assert annotation.startswith("::error title=Container acceptance failed::")
    assert '"service":"audio-check"' in annotation
    assert '"exit_code":1' in annotation
    assert "numbers_match" in annotation and "wer_above_0.15" in annotation
    assert "phrase_processing_exception" in annotation
    failed = public["audit"]["failed_phrases"][0]
    assert failed["key"] == "a" * 12 and failed["wer"] == .25
    assert public["audit"]["current_keys_match"] is True
    assert public["audit"]["voice_matches"] is True
    assert "<pre>" in rendered and "&quot;" in rendered


def test_annotation_escapes_commands_percent_and_line_breaks():
    escaped = ci.escape_annotation("100%\r\n::error::injected")
    assert escaped == "100%25%0D%0A::error::injected"
    assert "\n" not in escaped and "\r" not in escaped


def test_diagnostic_allowlist_handles_malformed_envelopes_without_secondary_exception():
    public = ci.public_diagnostics({
        "stage": ["PRIVATE_STAGE"], "compose_exit_code": "PRIVATE_EXIT",
        "states": [{"service": ["PRIVATE_SERVICE"], "state": {"Status": {}, "Health": {"Status": []},
                    "ExitCode": True, "OOMKilled": "PRIVATE_OOM"}}],
        "audit": {"status": {}, "phrases": [None, phrase(key={"PRIVATE_KEY": "bad"}, wer=float("nan"), rms=999999)], "count": False},
        "current": {"keys": [{}], "voice": "PRIVATE_VOICE"},
    })
    encoded = json.dumps(public, allow_nan=False)
    assert "PRIVATE_" not in encoded
    assert public["audit"]["invalid_phrase_entries"] == 1
    assert public["audit"]["current_keys_match"] is False
    assert public["containers"][0]["exit_code"] is None
    assert public["audit"]["failed_phrases"][0]["key"] == "invalid-key"


def test_unexpected_python_exception_still_fails_with_sanitized_annotation_and_summary(tmp_path, monkeypatch, capsys):
    summary_path = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    def fail(args, state):
        state.update(evidence())
        raise RuntimeError("PRIVATE_API_TOKEN and PRIVATE_PATIENT_NAME")
    monkeypatch.setattr(ci, "verify", fail)
    output = tmp_path / "run"
    assert ci.main(["--output", str(output)]) is True
    emitted = capsys.readouterr().out
    assert "PRIVATE_" not in emitted + summary_path.read_text()
    assert "::error" in emitted and '"unexpected_exception":"RuntimeError"' in emitted
    assert json.loads((output / "summary.json").read_text())["status"] == "failed"


def test_real_verifier_failure_publishes_structured_audit_reason_using_synthetic_docker(tmp_path, monkeypatch, capsys):
    fixture = evidence()
    commands = []
    states = []
    for name in ("orchestrator", "scenario-editor", "tts", "asr", "audio-init", "audio-check"):
        failed = name == "audio-check"
        exited = name in {"audio-init", "audio-check"}
        states.append({"Config": {"Labels": {"com.docker.compose.service": name}, "Env": ["PRIVATE_API_TOKEN"]},
                       "Image": "sha256:" + "b" * 64, "RestartCount": 0, "HostConfig": {"Memory": 512},
                       "State": {"Status": "exited" if exited else "running", "ExitCode": int(failed),
                                 "OOMKilled": False, "Health": {"Status": "healthy", "Log": [{"Output": "PRIVATE_LOG"}]}}})
    def fake_run(command, **kwargs):
        commands.append(command)
        text, code = "", 0
        if "config" in command:
            text = json.dumps(compose_configuration())
        elif command[:2] == ["docker", "inspect"]:
            text = json.dumps(states)
        elif "up" in command:
            assert "--wait" not in command and "--no-deps" not in command
            assert command[-2:] == ["orchestrator", "scenario-editor"]
            text, code = "Mandatory audio-check exited 1\n", 1
        elif "--quiet" in command:
            text = "container-id\n"
        elif "cp" in command:
            name = command[-2].rsplit("/", 1)[-1]
            Path(command[-1]).write_text(json.dumps(fixture["audit" if name == "audit.json" else "current"]))
        elif "logs" in command:
            text = "PRIVATE_CONTAINER_LOG_AND_PATIENT_TEXT"
        return subprocess.CompletedProcess(command, code, stdout=text, stderr="")
    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "step.md"))
    output = tmp_path / "run"
    assert ci.main(["--output", str(output), "--compose-file", "compose.yaml"]) is True
    emitted = capsys.readouterr().out
    annotation = next(line for line in emitted.splitlines() if line.startswith("::error"))
    assert "PRIVATE_" not in annotation + (tmp_path / "step.md").read_text()
    assert "audio-check" in annotation and "numbers_match" in annotation
    summary = json.loads((output / "summary.json").read_text())
    assert summary["compose_exit_code"] == 1 and summary["status"] == "failed"
    assert summary["public_diagnostics"]["audit"]["failed_phrase_count"] == 1
    assert not any("exec" in command for command in commands), "Failed mandatory audit must prevent later acceptance"


def test_unwritable_step_summary_cannot_mask_failure_or_report_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))
    monkeypatch.setattr(ci, "verify", lambda args, state: {"status": "passed", "failures": []})
    assert ci.main(["--output", str(tmp_path / "run")]) is True
    emitted = capsys.readouterr().out
    assert "::error" in emitted and '"summary_write_failed":true' in emitted
    assert json.loads((tmp_path / "run" / "summary.json").read_text())["status"] == "failed"


def test_asr_provider_mismatch_explains_expected_and_actual_without_raw_endpoint():
    public = ci.public_diagnostics({"stage": "asr-provider", "provider_failed": True,
                                    "expected_provider": "CPUExecutionProvider", "actual_provider": "CUDAExecutionProvider"})
    assert public["expected_provider"] == "CPUExecutionProvider"
    assert public["actual_provider"] == "CUDAExecutionProvider"
    assert public["provider_failed"] is True


@pytest.mark.parametrize("initial_code,wait_code,oneshot_exit,health,failed,missing,control_exit", [
    (0, 0, 0, "healthy", False, None, 0),
    (1, None, 0, "healthy", True, None, 0),
    (0, 1, 0, "healthy", True, None, 0),
    (0, 0, 2, "healthy", True, None, 0),
    (0, 0, 0, "unhealthy", True, None, 0),
    (0, 0, 0, "healthy", True, "asr", 0),
    (0, 0, 0, "healthy", True, None, 3),
])
def test_two_phase_startup_preserves_dependency_failure_and_inspects_oneshot_exit(
        tmp_path, monkeypatch, initial_code, wait_code, oneshot_exit, health, failed, missing, control_exit):
    """Synthetic Docker state, actual verifier control flow and gate decisions."""
    commands = []
    up_commands = []
    audit = {"status": "passed", "count": 1, "voice": "fixture-voice",
             "phrases": [{"key": "a" * 64, "status": "passed"}]}
    current = {"keys": ["a" * 64], "voice": "fixture-voice"}
    states = []
    for service in ("orchestrator", "scenario-editor", "tts", "asr", "audio-init", "audio-check", "control-init", "stand"):
        if service == missing:
            continue
        one_shot = service in {"audio-init", "audio-check", "control-init", "stand"}
        states.append({"Config": {"Labels": {"com.docker.compose.service": service}},
                       "Image": "sha256:" + "a" * 64, "RestartCount": 0,
                       "HostConfig": {"Memory": 512}, "State": {
                           "Status": "exited" if one_shot else "running",
                           "ExitCode": oneshot_exit if service == "audio-check" else control_exit if service == "control-init" else 0,
                           "Health": {"Status": "unhealthy" if service == "stand" else health if service == "asr" else "healthy"},
                           "OOMKilled": False}})
    def fake_run(command, **kwargs):
        commands.append(command)
        text, code = "", 0
        if "config" in command:
            configuration = compose_configuration({"control-init": {}})
            configuration["services"]["orchestrator"]["depends_on"]["control-init"] = {"condition": "service_completed_successfully"}
            text = json.dumps(configuration)
        elif command[:2] == ["docker", "inspect"]:
            text = json.dumps(states)
        elif "up" in command:
            up_commands.append(command)
            assert command[-2:] == ["orchestrator", "scenario-editor"]
            assert "--build" not in command
            if len(up_commands) == 1:
                assert "--wait" not in command and "--no-deps" not in command
                code = initial_code
            else:
                assert initial_code == 0
                assert "--no-deps" in command and "--wait" in command
                assert command[command.index("--wait-timeout") + 1] == "240"
                assert "audio-init" not in command and "audio-check" not in command
                code = wait_code
        elif "--quiet" in command:
            text = "fixture-id\n"
        elif "cp" in command:
            Path(command[-1]).write_text(json.dumps(audit if command[-2].endswith("audit.json") else current))
        elif "exec" in command:
            assert "wave.open" in command[-1], "Provider check must use a real bank WAV"
            text = json.dumps({"status": "ok", "payload": {"provider": "CPUExecutionProvider"}})
        return subprocess.CompletedProcess(command, code, stdout=text, stderr="")
    class ReadyResponse:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'{"ready":true}'
    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    monkeypatch.setattr(ci.urllib.request, "urlopen", lambda *args, **kwargs: ReadyResponse())
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "step-summary.md"))
    output = tmp_path / "run"
    assert ci.main(["--output", str(output), "--compose-file", "compose.yaml"]) is failed
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == ("failed" if failed else "passed")
    assert summary["initial_phase_exit_code"] == initial_code
    assert summary["wait_phase_exit_code"] == wait_code
    assert summary["compose_exit_code"] == (initial_code or wait_code)
    assert summary["public_diagnostics"]["initial_phase_exit_code"] == initial_code
    assert summary["public_diagnostics"]["wait_phase_exit_code"] == wait_code
    assert len(up_commands) == (1 if initial_code else 2)
    assert (output / "compose-up.txt").exists() and (output / "compose-wait.txt").exists()
    if oneshot_exit:
        assert any("audio-check: exited, exit 2" in message for message in summary["failures"])
    assert summary["dependency_service_count"] == 7 and summary["completed_job_count"] == 3
    assert summary["ignored_container_count"] == 1
    assert "stand" not in {item["service"] for item in json.loads((output / "container-states.json").read_text())}
    assert "stand" in {item["service"] for item in json.loads((output / "container-states-all.json").read_text())}
    if missing:
        assert any("Missing required services: asr" in message for message in summary["failures"])
    if control_exit:
        assert any("control-init: exited, exit 3" in message for message in summary["failures"])
    assert "PRIVATE_CONFIG_SECRET" not in (output / "summary.json").read_text()
    assert not list(output.glob("*config*")), "Resolved configuration must never be written"
    if initial_code or wait_code or oneshot_exit or health != "healthy" or missing or control_exit:
        assert not any("exec" in command for command in commands), "Failure must stop actual ASR acceptance"
    else:
        assert any("exec" in command for command in commands)


def test_dependency_closure_includes_only_targets_and_transitive_requirements():
    required, completed = ci.dependency_requirements(compose_configuration())
    assert required == {"orchestrator", "scenario-editor", "tts", "asr", "audio-init", "audio-check"}
    assert completed == {"audio-init", "audio-check"} and "stand" not in required


@pytest.mark.parametrize("mutation", ["missing-core-edge", "invalid-reference", "bad-json-structure"])
def test_configuration_cannot_omit_the_mandatory_audio_gate(mutation):
    configuration = compose_configuration()
    if mutation == "missing-core-edge":
        del configuration["services"]["orchestrator"]["depends_on"]["audio-check"]
    elif mutation == "invalid-reference":
        configuration["services"]["orchestrator"]["depends_on"]["missing"] = {}
    else:
        configuration["services"] = []
    with pytest.raises(ValueError):
        ci.dependency_requirements(configuration)


def test_configuration_failure_never_prints_or_persists_resolved_secrets(tmp_path, monkeypatch, capsys):
    def fake_run(command, **kwargs):
        text = "PRIVATE_RESOLVED_ENV_VALUE" if "config" in command else ""
        return subprocess.CompletedProcess(command, int("config" in command), stdout=text, stderr="PRIVATE_CONFIG_ERROR")
    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "step.md"))
    output = tmp_path / "run"
    assert ci.main(["--output", str(output)]) is True
    assert "PRIVATE_" not in capsys.readouterr().out + (tmp_path / "step.md").read_text()
    assert "PRIVATE_" not in "".join(path.read_text() for path in output.iterdir() if path.is_file())
    summary = json.loads((output / "summary.json").read_text())
    assert summary["public_diagnostics"]["dependency_config_failed"] is True
    assert summary["public_diagnostics"]["stage"] == "compose-config"


def test_lifecycle_state_drops_logs_and_preserves_absent_healthcheck_semantics():
    assert ci.lifecycle_state({"Status": "running", "ExitCode": 0, "OOMKilled": False,
                               "Error": "PRIVATE_DAEMON_ERROR"}) == {
        "Status": "running", "ExitCode": 0, "OOMKilled": False}
    result = ci.lifecycle_state({"Status": "running", "ExitCode": 0, "Health": {
        "Status": "healthy", "Log": [{"Output": "PRIVATE_HEALTH_LOG"}]}})
    assert result["Health"] == {"Status": "healthy"} and "PRIVATE_" not in json.dumps(result)

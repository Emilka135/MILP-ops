"""Public CI errors explain gate failures without publishing logs or phrase text."""

import importlib.util
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_container_diagnostics", ROOT / "debug/run_ci_container_check.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


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
        if command[:2] == ["docker", "inspect"]:
            text = json.dumps(states)
        elif "up" in command:
            assert "--wait" in command and command[command.index("--wait-timeout") + 1] == "240"
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

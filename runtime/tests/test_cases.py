"""The open test cases (cases/*.json): input (caller actions) → output (records in the stand)."""

from __future__ import annotations

import pytest

from calls.cases.runner import ROOT, LocalEnv, check_expectations, load_cases, run_case

CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(case, flows):
    r = run_case(case, flows)
    assert r.ok, "\n".join(r.problems) + f"\nwrites={[(w['method'], w['path'], w['status']) for w in r.writes]}"


def test_case_corpus_covers_every_write_kind():
    paths = {(w.get("method"), w.get("path", "").split("/")[-1]) for c in CASES for w in (c.get("expect") or {}).get("stand") or []}
    kinds = {"POST appointments", "DELETE {id}", "PATCH {id}", "POST confirm", "POST operator-requests"}
    seen = set()
    for m, last in paths:
        if last == "appointments" or last == "operator-requests" or last == "confirm":
            seen.add(f"{m} {last}")
        else:
            seen.add(f"{m} {{id}}")
    assert kinds <= seen, kinds - seen
    assert any(c["flow"] == "reminder" for c in CASES) and any(c["flow"] == "inbound" for c in CASES)


def test_check_expectations_catches_wrong_record():
    case = {"expect": {"status": "booked", "stand": [{"method": "POST", "path": "/api/v1/appointments", "body": {"patient_id": 1}}], "transfer": False}}
    final = {"status": "booked", "transfer": None, "vars": {}}
    ok_writes = [{"method": "POST", "path": "/api/v1/appointments", "status": 201, "body": {"patient_id": 1, "slot_id": 3}}]
    assert check_expectations(case, final, ok_writes, lambda rid: None) == []
    wrong_patient = [{"method": "POST", "path": "/api/v1/appointments", "status": 201, "body": {"patient_id": 2}}]
    assert any("lacks" in p for p in check_expectations(case, final, wrong_patient, lambda rid: None))
    extra = ok_writes + [{"method": "DELETE", "path": "/api/v1/appointments/APT-1", "status": 200}]
    assert any("2 stand writes" in p for p in check_expectations(case, final, extra, lambda rid: None))
    assert any("HTTP 409" in p for p in check_expectations(case, final, [{**ok_writes[0], "status": 409}], lambda rid: None))
    assert any("transfer" in p for p in check_expectations(case, {**final, "transfer": {"reason": "x"}}, ok_writes, lambda rid: None))


def test_local_env_is_isolated_per_case():
    case = {"id": "iso", "steps": [], "expect": {}}
    a = LocalEnv(case)
    b = LocalEnv(case)
    try:
        assert a.url != b.url and a.reg is not b.reg
    finally:
        a.close()
        b.close()


def test_remote_runner_imports_without_project_deps():
    """`tools/run_cases.py --orchestrator URL` runs on a bare interpreter (the compose e2e job installs nothing)."""
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['numpy'] = None; sys.path.insert(0, 'src');"
        "from calls.cases.runner import RemoteEnv, load_cases, run_all, format_table; print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr

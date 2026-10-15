import importlib
import subprocess
import sys

import pytest

from calls.scenario.compile import generate_mermaid, generate_python
from calls.scenario.dsl import FlowError, parse_flow, validate
from tests.conftest import ROOT

GOOD = '''
flow demo
  rate 1.5
  entry welcome
state welcome
  say "Нажмите 1 # не комментарий" rate=1.3   # комментарий
  dtmf 1|2 -> lookup
  dtmf 0 -> operator reason=requested
  timeout 30s -> welcome
  retry 2 -> operator reason=no_input
state lookup
  call crm.find_slots specialty=$specialty
  ok -> done
  empty -> operator reason=no_slots
  error -> operator reason=crm
state done
  say "Готово, {name}"
  sms booked
  end booked
state operator
  transfer reason=$reason
  end transfer
'''


def test_parse_good_flow():
    f = parse_flow(GOOD, "demo.flow")
    assert f.name == "demo" and f.entry == "welcome" and f.rate == 1.5
    w = f.states["welcome"]
    assert w.statements[0].text == "Нажмите 1 # не комментарий"
    assert w.statements[0].args == {"rate": "1.3"}
    assert [e.label for e in w.edges] == ["1|2", "0", "timeout 30s", "retry 2"]
    assert w.edges[1].sets == {"reason": "requested"}
    assert validate({"demo": f}) == []


@pytest.mark.parametrize(
    "snippet,msg",
    [
        ("state a\n  say \"x\"\n  dtmf 1 -> nowhere\n  timeout 5s -> a\n", "unknown state"),
        ("state a\n  say \"x\"\n  dtmf 1 -> a\n", "neither 'timeout' nor 'retry'"),
        ("state a\n  call crm.x\n  ok -> a\n", "without an 'error"),
        ("state a\n  say \"x\"\n  dtmf 1 -> a\n  timeout 5s -> a\nstate b\n  end\n", "unreachable"),
        ("state a\n  say \"x\"\n  any -> a\n  yes -> a\n  timeout 5s -> a\n", "'any' must be the last"),
        ("state a\n  say \"x\"\n", "no exit"),
        ("state a\n  switch x\n  case 1 -> a\n", "switch needs a 'default"),
        ("state a\n  say \"x\"\n  dtmf 1 -> a\n  dtmf 1 -> a\n  timeout 5s -> a\n", "duplicate edge"),
    ],
)
def test_validation_catches(snippet, msg):
    f = parse_flow("flow t\n  entry a\n" + snippet, "t.flow")
    problems = validate({"t": f})
    assert any(msg in p for p in problems), problems


@pytest.mark.parametrize("line", ["dtmf 12 -> a", "retry 0 -> a", "timeout 5x -> a", "frobnicate", "ask v digits x", "switch"])
def test_syntax_errors(line):
    with pytest.raises(FlowError):
        parse_flow(f"flow t\nstate a\n  {line}\n", "t.flow")


def test_repo_flows_valid(flows):
    assert validate(flows) == []
    assert set(flows) == {"inbound", "reminder", "reschedule"}


def test_compiled_artifacts_up_to_date():
    """The generated switch-case (and diagrams) must match the .flow sources – CI runs the same check."""
    res = subprocess.run([sys.executable, "-m", "calls.scenario.compile", "scenarios", "-o", "src/calls/scenario/compiled", "--mermaid", "docs/flows", "--check"],
                         cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_compiled_dispatch_matches_interpreter(flows):
    for name, flow in flows.items():
        mod = importlib.import_module(f"calls.scenario.compiled.{name}")
        assert mod.ENTRY == flow.entry
        for sname, st in flow.states.items():
            for e in st.edges:
                for v in e.values or ("",):
                    assert mod.dispatch(sname, e.kind, v) == e.target, (name, sname, e.label)
            assert mod.dispatch(sname, "nonexistent", "") is None
        assert mod.dispatch("no_such_state", "dtmf", "1") is None


def test_codegen_is_valid_python_and_mermaid():
    f = parse_flow(GOOD, "demo.flow")
    code = generate_python(f, GOOD)
    ns: dict = {}
    exec(compile(code, "demo_compiled.py", "exec"), ns)  # noqa: S102 - the generated module is the artefact under test
    assert ns["dispatch"]("welcome", "dtmf", "2") == "lookup"
    assert ns["dispatch"]("welcome", "retry", "") == "operator"
    mmd = generate_mermaid(f)
    assert mmd.startswith("flowchart TD") and "welcome -->|\"1|2\"| lookup" in mmd and "operator{{" in mmd

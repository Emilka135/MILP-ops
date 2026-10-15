"""Validate the shared flow DSL and emit real Rust match dispatch at build time."""

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "clinic_flow_dsl", ROOT / "runtime/src/calls/scenario/dsl.py"
)
dsl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dsl
spec.loader.exec_module(dsl)
load_flows, validate = dsl.load_flows, dsl.validate


def compile_to(directory: Path):
    flows = load_flows(ROOT / "scenarios")
    problems = validate(flows)
    if problems:
        raise ValueError("\n".join(problems))
    directory.mkdir(parents=True, exist_ok=True)
    nodes = []
    cases = []
    entries = []
    for name, flow in sorted(flows.items()):
        if not 1.3 <= flow.rate <= 1.75:
            raise ValueError(f"{name}: rate must be in 1.3..1.75")
        entries.append(f"        {json.dumps(name)} => Some({json.dumps(flow.entry)}),")
        for state_name, state in flow.states.items():
            cases.append(
                f"        ({json.dumps(name)}, {json.dumps(state_name)}) => Some({len(nodes)}),"
            )
            nodes.append({**dataclasses.asdict(state), "rate": flow.rate, "flow": name})
    # JSON is embedded once, parsed once. Transitions use generated Rust match.
    data = json.dumps(nodes, ensure_ascii=False, separators=(",", ":"))
    code = 'pub const NODES_JSON: &str = r####"' + data + '"####;\n'
    code += "pub fn state_index(flow: &str, state: &str) -> Option<usize> {\n    match (flow, state) {\n"
    code += "\n".join(cases) + "\n        _ => None,\n    }\n}\n"
    code += "pub fn entry(flow: &str) -> Option<&'static str> {\n    match flow {\n"
    code += "\n".join(entries) + "\n        _ => None,\n    }\n}\n"
    (directory / "scenarios.rs").write_text(code, encoding="utf-8")


if __name__ == "__main__":
    compile_to(Path(sys.argv[1]))

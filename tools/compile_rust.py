"""Validate the shared flow DSL and emit real Rust match dispatch at build time."""

import hashlib
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
    # System notices are emitted directly by Rust and must be banked too.
    notices=flows.get('system_notices')
    for key in ('emergency','complaint','record_opt_out','self_identity'):
        state=notices.states.get(key) if notices else None
        phrases=[st for st in state.statements if st.kind=='say'] if state else []
        if len(phrases)!=1 or not phrases[0].text or '{' in phrases[0].text:
            raise ValueError('system_notices.'+key+': exactly one static phrase required')
    problems = validate(flows)
    if problems:
        raise ValueError("\n".join(problems))
    directory.mkdir(parents=True, exist_ok=True)
    nodes = []
    cases = []
    entries = []
    for name, flow in sorted(flows.items()):
        if not 0.75 <= flow.rate <= 2.5:
            raise ValueError(f"{name}: rate must be in 0.75..2.5")
        entries.append(f"        {json.dumps(name)} => Some({json.dumps(flow.entry)}),")
        for state_name, state in flow.states.items():
            cases.append(
                f"        ({json.dumps(name)}, {json.dumps(state_name)}) => Some({len(nodes)}),"
            )
            nodes.append({**dataclasses.asdict(state), "rate": flow.rate, "flow": name})
    indexes={(n['flow'],n['name']):i for i,n in enumerate(nodes)}
    for n in nodes:
        for edge in n['edges']:
            target=edge['target'].split('.',1) if '.' in edge['target'] else (n['flow'],edge['target'])
            edge['target_index']=indexes[tuple(target)]
    # Immutable linked graph, parsed once. No per-call graph construction.
    data = json.dumps(nodes, ensure_ascii=False, separators=(",", ":"))
    code = 'pub const NODES_JSON: &str = r####"' + data + '"####;\n'
    code += f'pub const SCENARIO_REVISION: &str = "{hashlib.sha256(data.encode()).hexdigest()}";\n'
    code += "pub fn state_index(flow: &str, state: &str) -> Option<usize> {\n    match (flow, state) {\n"
    code += "\n".join(cases) + "\n        _ => None,\n    }\n}\n"
    code += "pub fn entry(flow: &str) -> Option<&'static str> {\n    match flow {\n"
    code += "\n".join(entries) + "\n        _ => None,\n    }\n}\n"
    (directory / "scenarios.rs").write_text(code, encoding="utf-8")


if __name__ == "__main__":
    compile_to(Path(sys.argv[1]))

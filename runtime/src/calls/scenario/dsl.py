"""``.flow`` – a line-oriented graph DSL for call scenarios.

Why not draw.io: the scenario is reviewed in pull requests, validated at build
time and compiled into a switch-case – it has to be *text*.  The format is a
flat list of states with statements and labelled edges::

    flow inbound
      rate 1.5
      entry welcome

    state welcome
      say "Здравствуйте! Нажмите 1 — запись, 2 — моя запись, 0 — оператор."
      dtmf 1 -> book
      dtmf 2 -> my_booking
      dtmf 0 -> operator reason=requested
      timeout 60s -> listen_free
      invalid -> welcome
      retry 2 -> operator reason=no_input

    state lookup
      call crm.find_patient snils=$snils
      ok -> ask_specialty
      empty -> operator reason=patient_not_found
      error -> operator reason=crm_error

Statements: ``say``, ``ask``, ``listen``, ``call``, ``set``, ``sms``,
``transfer``, ``end``, ``goto``, ``enter``, ``return``, ``switch <var>`` (with
``case value -> state`` / ``default -> state`` edges; ``case none`` = unset).
Edges: ``dtmf d|d``, ``yes``, ``no``, ``date``, ``time``, ``daypart``,
``digits``, ``intent a|b``, ``silence``, ``any``, ``timeout [Ns]``, ``invalid``,
``retry N`` (N-th failed input), ``loop N`` (N-th visit of the state), and
module statuses (``ok``, ``empty``, ``error``, ``both`` …).

:func:`parse_flow` builds the model, :func:`validate` enforces the structural
rules (every target exists, every waiting state has a timeout/retry exit, every
``call`` has an ``error`` edge, no unreachable states …) so a broken scenario
fails the build instead of a call.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

EDGE_KINDS = {"dtmf", "yes", "no", "date", "time", "daypart", "digits", "intent", "silence", "any", "timeout", "invalid", "retry", "loop", "case", "default"}
STATEMENTS = {"say", "ask", "listen", "call", "set", "sms", "transfer", "end", "goto", "enter", "return", "switch"}
_SPEECH_KINDS = ("yes", "no", "date", "time", "daypart", "digits", "intent", "any", "silence")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class FlowError(ValueError):
    def __init__(self, message: str, file: str = "", line: int = 0) -> None:
        where = f"{file}:{line}: " if file else ""
        super().__init__(where + message)
        self.file, self.line = file, line


@dataclass
class Statement:
    kind: str
    args: dict[str, str] = field(default_factory=dict)
    text: str = ""
    line: int = 0


@dataclass
class Edge:
    kind: str  # one of EDGE_KINDS or a module status
    values: tuple[str, ...]  # digits / intents, empty otherwise
    target: str
    sets: dict[str, str] = field(default_factory=dict)
    seconds: Optional[float] = None  # timeout
    count: Optional[int] = None  # retry
    line: int = 0

    @property
    def label(self) -> str:
        if self.kind == "dtmf":
            return "|".join(self.values)
        if self.kind == "intent":
            return "intent " + "|".join(self.values)
        if self.kind == "case":
            return "case " + self.values[0]
        if self.kind == "timeout" and self.seconds is not None:
            return f"timeout {self.seconds:g}s"
        if self.kind in ("retry", "loop"):
            return f"{self.kind} {self.count}"
        return self.kind


@dataclass
class State:
    name: str
    statements: list[Statement] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    line: int = 0

    def edge(self, kind: str) -> Optional[Edge]:
        return next((e for e in self.edges if e.kind == kind), None)

    @property
    def terminal(self) -> bool:
        return any(s.kind in ("end", "return") for s in self.statements)

    @property
    def waits_for_input(self) -> bool:
        return any(s.kind in ("ask", "listen") for s in self.statements) or any(
            e.kind in ("dtmf",) + _SPEECH_KINDS for e in self.edges
        )

    @property
    def waits_for_module(self) -> bool:
        return any(s.kind == "call" for s in self.statements)

    @property
    def listens(self) -> bool:
        return any(s.kind == "listen" for s in self.statements) or any(e.kind in _SPEECH_KINDS for e in self.edges)


@dataclass
class Flow:
    name: str
    entry: str = ""
    options: dict[str, str] = field(default_factory=dict)
    states: dict[str, State] = field(default_factory=dict)
    source: str = ""

    @property
    def rate(self) -> float:
        return float(self.options.get("rate", "1.0"))


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def _parse_seconds(tok: str, file: str, line: int) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(s|ms)?", tok)
    if not m:
        raise FlowError(f"bad duration {tok!r}", file, line)
    v = float(m.group(1))
    return v / 1000 if m.group(2) == "ms" else v


def _kv(tokens: Iterable[str], file: str, line: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in tokens:
        if "=" not in t:
            raise FlowError(f"expected key=value, got {t!r}", file, line)
        k, v = t.split("=", 1)
        out[k] = v
    return out


def _strip_comment(raw: str) -> str:
    """Remove a ``# comment`` that is outside double quotes."""
    out = []
    quote = ""
    escaped = False
    for ch in raw:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch in ('"', "'"):
            if quote == ch:
                quote = ""
            elif not quote:
                quote = ch
        if ch == "#" and not quote:
            break
        out.append(ch)
    return "".join(out).rstrip()


def parse_flow(text: str, source: str = "<string>") -> Flow:
    flow: Optional[Flow] = None
    state: Optional[State] = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as exc:
            raise FlowError(f"cannot tokenise: {exc}", source, lineno) from exc
        head = tokens[0]
        if head == "flow":
            if flow is not None:
                raise FlowError("only one 'flow' header per file", source, lineno)
            if len(tokens) != 2 or not _IDENT_RE.match(tokens[1]):
                raise FlowError("usage: flow <name>", source, lineno)
            flow = Flow(tokens[1], source=source)
            continue
        if flow is None:
            raise FlowError("file must start with 'flow <name>'", source, lineno)
        if head == "state":
            if len(tokens) != 2 or not _IDENT_RE.match(tokens[1]):
                raise FlowError("usage: state <name>", source, lineno)
            if tokens[1] in flow.states:
                raise FlowError(f"duplicate state {tokens[1]!r}", source, lineno)
            state = State(tokens[1], line=lineno)
            flow.states[state.name] = state
            continue
        if state is None:
            # flow option
            if len(tokens) < 2:
                raise FlowError("flow option needs a value", source, lineno)
            if head == "entry":
                flow.entry = tokens[1]
            else:
                flow.options[head] = " ".join(tokens[1:])
            continue
        # --- edges: "<kind> [values] -> target [k=v]"
        if "->" in tokens and head != "enter":
            arrow = tokens.index("->")
            lhs, rhs = tokens[:arrow], tokens[arrow + 1 :]
            if not lhs or not rhs:
                raise FlowError("edge needs a kind and a target", source, lineno)
            kind = lhs[0]
            target = rhs[0]
            sets = _kv(rhs[1:], source, lineno)
            values: tuple[str, ...] = ()
            seconds: Optional[float] = None
            count: Optional[int] = None
            if kind == "dtmf":
                if len(lhs) != 2:
                    raise FlowError("usage: dtmf <digits> -> target", source, lineno)
                values = tuple(lhs[1].split("|"))
                for d in values:
                    if d not in "0123456789*#" or len(d) != 1:
                        raise FlowError(f"bad DTMF digit {d!r}", source, lineno)
            elif kind == "intent":
                if len(lhs) != 2:
                    raise FlowError("usage: intent a|b -> target", source, lineno)
                values = tuple(lhs[1].split("|"))
            elif kind == "timeout":
                if len(lhs) == 2:
                    seconds = _parse_seconds(lhs[1], source, lineno)
                elif len(lhs) != 1:
                    raise FlowError("usage: timeout [Ns] -> target", source, lineno)
            elif kind == "case":
                if len(lhs) != 2:
                    raise FlowError("usage: case <value> -> target", source, lineno)
                values = (lhs[1],)
            elif kind in ("retry", "loop"):
                if len(lhs) != 2 or not lhs[1].isdigit() or int(lhs[1]) < 1:
                    raise FlowError(f"usage: {kind} <n> -> target", source, lineno)
                count = int(lhs[1])
            elif len(lhs) != 1:
                raise FlowError(f"edge {kind!r} takes no arguments", source, lineno)
            elif not _IDENT_RE.match(kind):
                raise FlowError(f"bad edge kind {kind!r}", source, lineno)
            state.edges.append(Edge(kind, values, target, sets, seconds, count, lineno))
            continue
        # --- statements
        if head not in STATEMENTS:
            raise FlowError(f"unknown statement {head!r}", source, lineno)
        st = Statement(head, line=lineno)
        if head == "say":
            if len(tokens) < 2:
                raise FlowError("say needs a quoted text", source, lineno)
            st.text = tokens[1]
            st.args = _kv(tokens[2:], source, lineno)
        elif head == "ask":
            # ask <var> digits <min>[-<max>] [timeout Ns] [terminator #]
            if len(tokens) < 4 or tokens[2] != "digits":
                raise FlowError("usage: ask <var> digits <n>[-<m>] [timeout Ns] [terminator #]", source, lineno)
            st.args["var"] = tokens[1]
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", tokens[3])
            if not m:
                raise FlowError("ask: digits must be n or n-m", source, lineno)
            st.args["min"] = m.group(1)
            st.args["max"] = m.group(2) or m.group(1)
            rest = tokens[4:]
            i = 0
            while i < len(rest):
                if rest[i] == "timeout" and i + 1 < len(rest):
                    st.args["timeout"] = str(_parse_seconds(rest[i + 1], source, lineno))
                    i += 2
                elif rest[i] == "terminator" and i + 1 < len(rest):
                    st.args["terminator"] = rest[i + 1]
                    i += 2
                else:
                    raise FlowError(f"ask: unexpected {rest[i]!r}", source, lineno)
        elif head == "listen":
            if len(tokens) > 3:
                raise FlowError("usage: listen [Ns] [field=full_name|free_text]",source,lineno)
            st.args["timeout"] = str(_parse_seconds(tokens[1],source,lineno)) if len(tokens)>=2 else "8"
            if len(tokens)==3:
                fields=_kv(tokens[2:],source,lineno)
                if set(fields)!={"field"} or fields["field"] not in {"full_name","free_text"}:
                    raise FlowError("invalid speech field",source,lineno)
                st.args.update(fields)
        elif head == "call":
            if len(tokens) < 2 or "." not in tokens[1]:
                raise FlowError("usage: call <module>.<op> [k=v ...]", source, lineno)
            st.args["module"], st.args["op"] = tokens[1].split(".", 1)
            st.args.update({f"arg:{k}": v for k, v in _kv(tokens[2:], source, lineno).items()})
        elif head == "set":
            if len(tokens) != 4 or tokens[2] != "=":
                raise FlowError("usage: set <var> = <value>", source, lineno)
            st.args["var"], st.args["value"] = tokens[1], tokens[3]
        elif head == "sms":
            if len(tokens) != 2:
                raise FlowError("usage: sms <template>", source, lineno)
            st.args["template"] = tokens[1]
        elif head == "transfer":
            st.args = _kv(tokens[1:], source, lineno)
        elif head == "end":
            st.args["status"] = tokens[1] if len(tokens) > 1 else "done"
        elif head == "goto":
            if len(tokens) != 2:
                raise FlowError("usage: goto <state>", source, lineno)
            st.args["target"] = tokens[1]
        elif head == "enter":
            if len(tokens) != 4 or tokens[2] != "->":
                raise FlowError("usage: enter <flow.state|state> -> <return_state>", source, lineno)
            st.args["target"], st.args["return"] = tokens[1], tokens[3]
        elif head == "return":
            if len(tokens) != 1:
                raise FlowError("usage: return", source, lineno)
        elif head == "switch":
            if len(tokens) != 2:
                raise FlowError("usage: switch <var>", source, lineno)
            st.args["var"] = tokens[1]
        state.statements.append(st)
    if flow is None:
        raise FlowError("empty flow file", source, 0)
    if not flow.entry:
        flow.entry = next(iter(flow.states), "")
    return flow


def load_flow(path: Path | str) -> Flow:
    p = Path(path)
    return parse_flow(p.read_text(encoding="utf-8"), str(p))


def load_flows(directory: Path | str) -> dict[str, Flow]:
    flows = {}
    for p in sorted(Path(directory).glob("*.flow")):
        f = load_flow(p)
        if f.name in flows:
            raise FlowError(f"duplicate flow name {f.name!r}", str(p), 1)
        flows[f.name] = f
    return flows


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def _resolve(flows: dict[str, Flow], flow: Flow, ref: str) -> bool:
    if "." in ref:
        fname, sname = ref.split(".", 1)
        return fname in flows and sname in flows[fname].states
    return ref in flow.states


def validate(flows: dict[str, Flow]) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems: list[str] = []
    for flow in flows.values():
        src = flow.source

        def err(msg: str, line: int, _src: str = src, _name: str = flow.name) -> None:
            problems.append(f"{_src}:{line}: [{_name}] {msg}")

        if flow.entry not in flow.states:
            err(f"entry state {flow.entry!r} does not exist", 1)
        try:
            float(flow.options.get("rate", "1.0"))
        except ValueError:
            err("rate must be a number", 1)
        reachable: set[str] = set()
        stack = [flow.entry] if flow.entry in flow.states else []
        for other in flows.values():  # states entered from other flows are reachable too
            for st in other.states.values():
                for s in st.statements:
                    if s.kind == "enter" and s.args["target"].startswith(flow.name + "."):
                        stack.append(s.args["target"].split(".", 1)[1])
        while stack:
            cur = stack.pop()
            if cur in reachable or cur not in flow.states:
                continue
            reachable.add(cur)
            st = flow.states[cur]
            for e in st.edges:
                stack.append(e.target)
            for s in st.statements:
                if s.kind == "goto":
                    stack.append(s.args["target"])
                if s.kind == "enter":
                    stack.append(s.args["return"])
                    if "." not in s.args["target"]:
                        stack.append(s.args["target"])
        for name, st in flow.states.items():
            if name not in reachable:
                err(f"state {name!r} is unreachable", st.line)
            kinds = [s.kind for s in st.statements]
            for e in st.edges:
                if not _resolve(flows, flow, e.target):
                    err(f"edge '{e.label}' points to unknown state {e.target!r}", e.line)
            for s in st.statements:
                if s.kind in ("goto",) and not _resolve(flows, flow, s.args["target"]):
                    err(f"goto unknown state {s.args['target']!r}", s.line)
                if s.kind == "enter":
                    if not _resolve(flows, flow, s.args["target"]):
                        err(f"enter unknown state {s.args['target']!r}", s.line)
                    if s.args["return"] not in flow.states:
                        err(f"enter return state {s.args['return']!r} unknown", s.line)
                if s.kind == "say":
                    for ph in _PLACEHOLDER_RE.findall(s.text):
                        if not _IDENT_RE.match(ph):
                            err(f"bad placeholder {{{ph}}}", s.line)
                    if "{" in s.text.replace("{", "", s.text.count("{")) or s.text.count("{") != s.text.count("}"):
                        err("unbalanced braces in say", s.line)
                if s.kind == "call" and st.edge("error") is None:
                    err("call without an 'error -> …' edge", s.line)
            if "switch" in kinds and st.edge("default") is None:
                err("switch needs a 'default -> …' edge", st.line)
            if "switch" not in kinds and (st.edge("case") is not None or st.edge("default") is not None):
                err("case/default edges require a 'switch <var>' statement", st.line)
            terminal_kinds = {"end", "return", "goto", "enter", "switch"}
            has_exit = bool(st.edges) or any(k in terminal_kinds for k in kinds)
            if not has_exit:
                err("state has no exit (no edges, goto, enter, end or return)", st.line)
            if "transfer" in kinds and not any(k in ("end", "goto", "enter", "return") for k in kinds):
                err("transfer must be followed by end/goto", st.line)
            if st.waits_for_input:
                if st.edge("timeout") is None and st.edge("retry") is None and "ask" not in kinds:
                    err("state waits for input but has neither 'timeout' nor 'retry' edge", st.line)
                if "ask" in kinds and st.edge("ok") is None:
                    err("ask needs an 'ok -> …' edge", st.line)
            if st.waits_for_module and st.edge("ok") is None and not any(e.kind not in EDGE_KINDS for e in st.edges):
                err("call needs at least an 'ok' or status edge", st.line)
            if st.waits_for_module and st.waits_for_input:
                err("a state cannot both call a module and wait for user input", st.line)
            speech_edges = [e for e in st.edges if e.kind in _SPEECH_KINDS and e.kind != "silence"]
            if speech_edges and any(e.kind == "any" for e in speech_edges) and speech_edges[-1].kind != "any":
                err("'any' must be the last speech edge in the state", next(e for e in speech_edges if e.kind == "any").line)
            seen: set[str] = set()
            for e in st.edges:
                for v in e.values or (e.kind,):
                    key = f"{e.kind}:{v}"
                    if key in seen:
                        err(f"duplicate edge {e.label!r}", e.line)
                    seen.add(key)
            if "ask" in kinds and st.edge("dtmf") is not None:
                err("ask (digit collection) and dtmf menu edges cannot coexist", st.line)
    return problems

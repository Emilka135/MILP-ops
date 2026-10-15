"""In-process call simulator: scenario engine + CRM module + a scripted caller.

Used by the tests ("generate text → corrupt as ASR would → normalise → classify →
switch-case → expected CRM writes") and by ``tools/bench_nlu.py``.  No threads,
no audio, no clock – a full call runs in well under a millisecond.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Optional, Union

from calls.crm.module import CrmModule
from calls.scenario.dsl import Flow
from calls.scenario.engine import Action, Event, NluFn, Session, default_nlu

CallerInput = Union[str, tuple[str, str]]  # "1" (dtmf), ("say", "text"), ("digits", "12345678901"), ("timeout",), ("silence",)


@dataclass
class CallTrace:
    prompts: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    module_calls: list[tuple[str, str, dict, str]] = field(default_factory=list)  # module, op, args, status
    status: Optional[str] = None
    transfer: Optional[dict] = None
    sms: list[dict] = field(default_factory=list)
    vars: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    unused_inputs: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.prompts)


def run_call(
    flows: dict[str, Flow],
    flow: str,
    crm: CrmModule,
    inputs: Iterable[CallerInput],
    vars: Optional[dict] = None,
    today: Optional[date] = None,
    nlu: NluFn = default_nlu,
    asr: Optional[Callable[[str], str]] = None,
    max_steps: int = 400,
) -> CallTrace:
    """Drive one call to completion.  ``asr`` optionally corrupts spoken inputs (simulated recogniser)."""
    session = Session(flows, flow, vars=vars, today=today, nlu=nlu)
    queue = list(inputs)
    trace = CallTrace()

    def execute(actions: list[Action]) -> None:
        steps = 0
        pending_input = False
        while actions:
            steps += 1
            if steps > max_steps:
                raise RuntimeError("simulation did not converge")
            nxt: list[Action] = []
            for a in actions:
                trace.actions.append(a)
                if a.kind == "say":
                    trace.prompts.append(a.text)
                elif a.kind == "call":
                    status, payload = crm.handle(a.args["op"], a.args["args"])
                    trace.module_calls.append((a.args["module"], a.args["op"], dict(a.args["args"]), status))
                    nxt.extend(session.handle(Event("module", status, payload, request_id=a.request_id)))
                elif a.kind in ("collect", "listen", "timer"):
                    pending_input = True
                elif a.kind == "transfer":
                    trace.transfer = a.args
                elif a.kind == "sms":
                    trace.sms.append(a.args)
                elif a.kind == "end":
                    trace.status = a.args["status"]
            actions = nxt
            if not actions and pending_input and not session.finished:
                pending_input = False
                if not queue:
                    return
                item = queue.pop(0)
                actions = feed(item)

    def feed(item: CallerInput) -> list[Action]:
        if isinstance(item, str):
            out: list[Action] = []
            for ch in item:  # "1" or a digit string typed on the keypad
                out.extend(session.handle(Event("dtmf", ch)))
                if session.finished or session.state_name != state_before(out):
                    pass
            return out
        kind = item[0]
        if kind == "say":
            text = item[1]
            if asr is not None:
                text = asr(text)
            return session.handle(Event("speech", text))
        if kind == "digits":
            out = []
            for ch in item[1]:
                out.extend(session.handle(Event("dtmf", ch)))
            return out
        if kind == "timeout":
            return session.handle(Event("timeout"))
        if kind == "silence":
            return session.handle(Event("silence"))
        if kind == "hangup":
            return session.handle(Event("hangup"))
        raise ValueError(f"unknown caller input {item!r}")

    def state_before(_: list[Action]) -> str:
        return session.state_name

    execute(session.start())
    trace.states = list(session.history)
    trace.transfer = trace.transfer or session.transfer
    trace.sms = trace.sms or session.sms_sent
    trace.vars = dict(session.vars)
    trace.warnings = list(session.warnings)
    trace.unused_inputs = queue
    if trace.status is None:
        trace.status = session.status
    return trace

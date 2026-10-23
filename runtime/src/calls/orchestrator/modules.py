"""Module adapters for the bus and a factory that wires a complete in-process stack.

Each adapter has the same ``handle(op, args) -> (status, payload)`` signature,
so a module can live in-process (tests, single box) or behind
``calls.services.http`` in its own container (``HttpModuleClient``) without the
orchestrator noticing.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from calls.crm.module import CrmModule
from calls.crm.operator import build_ticket
from calls.crm.registry import MockRegistry
from calls.services.netutil import post_json
from calls.nlu.agent import TwoAgentNLU
from calls.nlu.intent import IntentResult, classify
from calls.nlu.normalize import interpret
from calls.notify.sms import MockSmsGateway, SmsModule
from calls.scenario.engine import NluOutcome
from calls.tts.silero import StubTTS, Synth, TtsModule

from .bus import Bus, ModuleSpec


# ---- NLU ----------------------------------------------------------------------------------------


@dataclass
class NluModule:
    """interpret + classify (rules) + two-agent decision; returns the NluOutcome for the engine."""

    agent: TwoAgentNLU = field(default_factory=TwoAgentNLU)

    def handle(self, op: str, args: dict) -> tuple[str, dict]:
        text = str(args.get("text", ""))
        today = date.fromisoformat(args["today"]) if args.get("today") else date.today()
        if op == "interpret":
            interp = interpret(text, today)
            rules = classify(text, today, self.agent.known_surnames)
            decision = self.agent.decide(text, today, have=args.get("have", ()))
            if rules.intent == "unknown" and decision.intent != "unknown" and decision.action != "clarify":
                rules = IntentResult(decision.intent, decision.confidence, rules.scores, rules.entities, ["embed"])
            elif decision.action == "operator" and rules.intent != decision.intent:
                rules = IntentResult(decision.intent, max(rules.confidence, decision.confidence), rules.scores, rules.entities, ["agent"])
            return "ok", {"outcome": NluOutcome(interp, rules), "decision": decision.action, "intent": rules.intent,
                          "confidence": rules.confidence, "normalized": interp.normalized}
        if op == "decide":
            d = self.agent.decide(text, today, have=args.get("have", ()))
            return "ok", {"intent": d.intent, "action": d.action, "confidence": d.confidence, "reasons": d.reasons, "missing": d.missing}
        return "error", {"error": f"unknown op {op}"}


# ---- ASR ----------------------------------------------------------------------------------------


@dataclass
class AsrModule:
    """Gateway to the streaming recogniser.  Here: a stub that returns the text it is given.

    In production this adapter wraps the Deeray/GigaAM v3 runtime WebSocket (see docs/ARCHITECTURE.md);
    the orchestrator only ever sees ``{"text": ..., "final": True}``.
    """

    engine: str = "stub"

    def handle(self, op: str, args: dict) -> tuple[str, dict]:
        if op == "transcribe":
            return "ok", {"text": args.get("text", ""), "final": True, "engine": self.engine, "rtf": 0.0}
        return "error", {"error": f"unknown op {op}"}


# ---- operator ----------------------------------------------------------------------------------


@dataclass
class OperatorModule:
    reg: MockRegistry
    on_ticket: Optional[Callable[[dict], None]] = None  # → outbox: POST to the stand's "transfer to operator" API
    tickets: list = field(default_factory=list)

    def handle(self, op: str, args: dict) -> tuple[str, dict]:
        if op != "transfer":
            return "error", {"error": f"unknown op {op}"}
        ticket = build_ticket(self.reg, args.get("call_id", ""), args.get("reason") or "unspecified", args.get("vars", {}), args.get("history", []), args.get("utterances", []), user_state=args.get("user_state"))
        self.tickets.append(ticket)
        if self.on_ticket:
            self.on_ticket({"call_id": ticket.call_id, "reason": ticket.reason, "patient_id": ticket.patient_id, "booking_id": ticket.booking_id,
                            "constraints": ticket.constraints, "request_id": ticket.request_id, "summary": ticket.summary(),
                            "user_state": (ticket.user_state or {}).get("label")})
        return "ok", {"ticket_id": len(self.tickets), "highlighted": len(ticket.highlighted_slot_ids), "html": ticket.to_html(), "json": ticket.to_json()}


# ---- remote module client -------------------------------------------------------------------------


@dataclass
class HttpModuleClient:
    """Bus handler that forwards to ``calls.services.http`` running elsewhere (another container)."""

    base_url: str
    timeout: float = 5.0

    def handle(self, op: str, args: dict) -> tuple[str, dict]:
        data = post_json(f"{self.base_url.rstrip('/')}/v1/{op}", args, timeout=self.timeout)
        payload = data.get("payload", {})
        for k in [k for k in payload if k.endswith("_b64")]:  # binary fields come back as base64 (see services/http.py)
            try:
                payload[k[:-4]] = base64.b64decode(payload.pop(k))
            except (ValueError, TypeError):
                pass
        return data.get("status", "error"), payload


# ---- factory ----------------------------------------------------------------------------------------


@dataclass
class Stack:
    bus: Bus
    reg: MockRegistry
    crm: CrmModule
    tts: TtsModule
    nlu: NluModule
    sms: SmsModule
    operator: OperatorModule
    sms_gateway: MockSmsGateway


def build_stack(reg: MockRegistry, today: Callable[[], date] = date.today, synth: Optional[Synth] = None, clinic: Optional[dict] = None,
                workers: Optional[dict[str, int]] = None, on_write: Optional[Callable[[str, dict], None]] = None) -> Stack:
    workers = {"crm": 2, "tts": 2, "nlu": 2, "sms": 1, "operator": 1, "asr": 2, **(workers or {})}
    gateway = MockSmsGateway()

    def phone_for(vars: dict) -> Optional[str]:
        pid = vars.get("patient_id")
        p = reg.patient_by_id(int(pid)) if pid else None
        return p.phone if p else None

    crm = CrmModule(reg, today=today, on_write=on_write)
    tts = TtsModule(synth or StubTTS())
    nlu = NluModule()
    sms = SmsModule(gateway, phone_for, defaults=clinic or {})
    operator = OperatorModule(reg, on_ticket=(lambda t: on_write("transfer", t)) if on_write else None)
    bus = Bus()
    bus.register(ModuleSpec("crm", crm.handle, workers["crm"], default_timeout=5.0))
    bus.register(ModuleSpec("tts", tts.handle, workers["tts"], default_timeout=4.0))
    bus.register(ModuleSpec("nlu", nlu.handle, workers["nlu"], default_timeout=2.0))
    bus.register(ModuleSpec("sms", sms.handle, workers["sms"], default_timeout=30.0))
    bus.register(ModuleSpec("operator", operator.handle, workers["operator"], default_timeout=10.0))
    bus.register(ModuleSpec("asr", AsrModule().handle, workers["asr"], default_timeout=3.0))
    return Stack(bus, reg, crm, tts, nlu, sms, operator, gateway)

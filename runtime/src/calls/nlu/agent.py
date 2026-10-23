"""Two-agent decision: classify → verify → execute | clarify | operator.

Agent 1 ("classifier") = rules (``intent.py``) backed by the embedding model
(``embed.py``).  Agent 2 ("verifier") re-derives the intent independently and
only lets the request through to the CRM when both agree *and* the request is
fulfilable from the data we have.  Anything else is escalated to a human with
the full context – the case says "zero booking errors" beats "share solved
without an operator".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from .embed import EmbeddingIntentClassifier, HashEmbedder
from .intent import HUMAN_ONLY, SELF_SERVICE, IntentResult, classify


@dataclass
class Decision:
    intent: str
    action: str  # "execute" | "clarify" | "operator"
    confidence: float
    reasons: list[str] = field(default_factory=list)
    rules: Optional[IntentResult] = None
    embed_intent: Optional[str] = None
    embed_similarity: float = 0.0
    missing: list[str] = field(default_factory=list)

    @property
    def to_operator(self) -> bool:
        return self.action == "operator"


@dataclass
class TwoAgentNLU:
    embed: EmbeddingIntentClassifier = field(default_factory=lambda: EmbeddingIntentClassifier(HashEmbedder()).fit())
    rule_accept: float = 0.75
    known_surnames: tuple[str, ...] = ()

    def classify(self, text: str, today: Optional[date] = None) -> tuple[IntentResult, str, float]:
        rules = classify(text, today, self.known_surnames)
        pred = self.embed.predict(text)
        return rules, pred.intent, pred.similarity

    def decide(self, text: str, today: Optional[date] = None, have: Iterable[str] = ()) -> Decision:
        """``have`` lists facts already known in the session (e.g. "patient", "booking")."""
        have_set = set(have)
        rules, e_intent, e_sim = self.classify(text, today)
        reasons: list[str] = []
        intent = "unknown"
        conf = 0.0

        if rules.intent in HUMAN_ONLY and rules.confidence >= 0.6:
            # safety first: a complaint / pain / medication question never waits for a verifier
            reasons.append(f"rules:{rules.intent}@{rules.confidence}", )
            reasons.append("human-only intent")
            return Decision(rules.intent, "operator", rules.confidence, reasons, rules, e_intent, e_sim)
        if rules.entities.doctor_surname and not rules.entities.specialty and rules.intent in ("book", "unknown"):
            # a specific surname the switch-case cannot resolve safely → human, whatever the confidence
            reasons.append(f"rules:{rules.intent}@{rules.confidence}")
            reasons.append("specific doctor requested")
            return Decision("book", "operator", max(rules.confidence, 0.6), reasons, rules, e_intent, e_sim)
        if rules.intent != "unknown" and rules.confidence >= self.rule_accept:
            intent, conf = rules.intent, rules.confidence
            reasons.append(f"rules:{rules.intent}@{rules.confidence}")
            if e_intent not in ("unknown", intent):
                # verifier disagrees – only trust rules when they are very sure
                if rules.confidence < 0.9:
                    reasons.append(f"verifier disagrees ({e_intent}) → clarify")
                    return Decision(intent, "clarify", conf, reasons, rules, e_intent, e_sim)
                reasons.append(f"verifier disagrees ({e_intent}) but rules are confident")
        elif e_intent != "unknown":
            if rules.intent == e_intent or rules.intent == "unknown":
                intent, conf = e_intent, max(e_sim, rules.confidence)
                reasons.append(f"embed:{e_intent}@{e_sim}")
            else:
                reasons.append(f"rules {rules.intent} vs embed {e_intent} → clarify")
                return Decision(rules.intent, "clarify", rules.confidence, reasons, rules, e_intent, e_sim)
        else:
            reasons.append("nobody is sure")
            return Decision("unknown", "clarify", 0.0, reasons, rules, e_intent, e_sim)

        if intent in HUMAN_ONLY:
            reasons.append("human-only intent")
            return Decision(intent, "operator", conf, reasons, rules, e_intent, e_sim)

        missing: list[str] = []
        ents = rules.entities
        if intent == "book":
            if ents.doctor_surname and not ents.specialty:
                # a specific surname the switch-case cannot resolve safely → human
                reasons.append("specific doctor requested")
                return Decision(intent, "operator", conf, reasons, rules, e_intent, e_sim)
            if not ents.specialty:
                missing.append("specialty")
        if intent in ("reschedule", "cancel", "confirm", "my_booking") and "booking" not in have_set:
            missing.append("booking")
        if intent in SELF_SERVICE and intent.startswith(("book", "reschedule", "cancel", "confirm", "my_booking")) and "patient" not in have_set:
            missing.append("patient")
        action = "execute" if not missing else "clarify"
        return Decision(intent, action, conf, reasons, rules, e_intent, e_sim, missing)

"""Call runner: executes scenario actions on the module bus, feeds results back as events.

One ``CallRunner`` per live call.  Media (Asterisk ARI / AudioSocket) calls
``on_dtmf`` / ``on_speech`` / ``on_silence`` / ``on_hangup`` from its own
threads; everything is serialised per call by ``_lock``, module results arrive
on bus worker threads and are re-injected through the same lock.  Prompts are
synthesised by the ``tts`` module (per-call ``rate``); timers are real
``threading.Timer`` objects armed only after the prompt has been played
(``prompt_done`` is reported by the media layer, or estimated).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Callable, Optional

from calls.nlu.early import EarlyDecision, EarlyIntentTracker
from calls.scenario.dsl import Flow
from calls.scenario.engine import Action, Event, NluOutcome, Session, default_nlu

from .bus import Bus, Overloaded, Response

log = logging.getLogger(__name__)


@dataclass
class MediaSink:
    """What the runner needs from telephony; the default records everything (tests / dry-run)."""

    play: Callable[[str, dict], None] = lambda call_id, clip: None
    stop: Callable[[str], None] = lambda call_id: None
    transfer: Callable[[str, dict], None] = lambda call_id, ticket: None
    hangup: Callable[[str, str], None] = lambda call_id, status: None


@dataclass
class CallRecord:
    call_id: str
    flow: str
    started: float = field(default_factory=time.monotonic)
    prompts: list[str] = field(default_factory=list)
    clips: list[dict] = field(default_factory=list)
    module_calls: list[Response] = field(default_factory=list)
    sms: list[dict] = field(default_factory=list)
    transfer: Optional[dict] = None
    status: Optional[str] = None
    ended: Optional[float] = None
    utterances: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    early: list[dict] = field(default_factory=list)  # early intent / caller-state decisions, one per utterance
    user_state: Optional[str] = None  # latest caller state (calm / hurried / agitated / distressed / confused)

    @property
    def duration_s(self) -> float:
        return ((self.ended or time.monotonic()) - self.started)


class CallRunner:
    def __init__(
        self,
        call_id: str,
        flows: dict[str, Flow],
        flow: str,
        bus: Bus,
        vars: Optional[dict] = None,
        today: Optional[date] = None,
        media: Optional[MediaSink] = None,
        on_finish: Optional[Callable[["CallRunner"], None]] = None,
        speech_chars_per_s: float = 15.0,
        use_nlu_module: bool = True,
        early_deadline_s: float = 3.0,
    ) -> None:
        self.call_id = call_id
        self.bus = bus
        self.media = media or MediaSink()
        self.record = CallRecord(call_id, flow)
        self.session = Session(flows, flow, vars=vars, today=today, nlu=self._nlu)
        self.on_finish = on_finish
        self.speech_chars_per_s = speech_chars_per_s
        self.use_nlu_module = use_nlu_module and "nlu" in bus.modules()
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._collect: Optional[dict] = None
        self._listening = False
        self._generation = 0  # invalidates stale timers / module results
        self._pending_nlu: Optional[NluOutcome] = None
        self.early_deadline_s = early_deadline_s
        self._early: Optional[EarlyIntentTracker] = None  # one tracker per listening phase (utterance)
        self._early_acted = False  # the early decision already injected a speech event for this utterance

    # ---- lifecycle -------------------------------------------------------------------

    def start(self) -> "CallRunner":
        with self._lock:
            self._execute(self.session.start())
        return self

    @property
    def finished(self) -> bool:
        return self.session.finished

    # ---- media → events ---------------------------------------------------------------

    def on_dtmf(self, digit: str) -> None:
        self._inject(Event("dtmf", digit))

    def on_speech(self, text: str) -> None:
        if self._early is not None and not self._early.decided:
            self._note_early(self._early.on_final(self._now_ms(), text))
        if self._early_acted:
            # the utterance was already handled from its partials (early escalation); drop the duplicate final
            self._early_acted = False
            self._early = None
            return
        self._early = None
        self.record.utterances.append(text)
        if self.use_nlu_module:
            fut = self.bus.submit("nlu", "interpret", {"text": text, "today": self.session.today.isoformat()}, self.call_id, timeout=2.0)
            fut.add_done_callback(lambda f: self._on_nlu_done(f.result(), text))
        else:
            self._inject(Event("speech", text))

    def on_silence(self) -> None:
        self._inject(Event("silence"))

    # ---- streaming ASR: partial hypotheses + audio → early intent / caller state --------------------------------

    def on_audio(self, pcm: bytes) -> None:
        """Raw caller audio (8 kHz PCM16) while listening → prosody features for the caller state."""
        tracker = self._tracker()
        if tracker is not None:
            tracker.on_audio(pcm)

    def on_partial(self, text: str, at_ms: Optional[float] = None, stable_text: str = "") -> Optional[EarlyDecision]:
        """Streaming partial of the open utterance.  Decides early when the evidence is conclusive; an
        escalation (pain, complaint, operator request) is injected as a speech event right away – the caller
        does not have to finish the sentence or wait for end-pointing."""
        tracker = self._tracker()
        if tracker is None:
            return None
        at = self._now_ms() if at_ms is None else float(at_ms)
        already = tracker.decided
        decision = tracker.on_partial(at, text, stable_text) or tracker.tick(at)
        if decision is None or already:
            return decision
        self._note_early(decision)
        if decision.escalate and not self._early_acted:
            self._early_acted = True
            out = default_nlu(decision.text, self.session.today)
            self._pending_nlu = NluOutcome(out.interpretation, replace(out.intent, intent=decision.intent, confidence=max(out.intent.confidence, decision.confidence)))
            self.record.utterances.append(decision.text)
            self._inject(Event("speech", decision.text))
        return decision

    def _tracker(self) -> Optional[EarlyIntentTracker]:
        if not self.listening:
            return None
        if self._early is None:
            self._early = EarlyIntentTracker(deadline_s=self.early_deadline_s)
            self._early_acted = False
        return self._early

    def _note_early(self, decision: EarlyDecision) -> None:
        d = decision.as_dict()
        d["state"] = self.session.state_name
        self.record.early.append(d)
        self.record.user_state = decision.user_state.label

    def _now_ms(self) -> float:
        return (time.monotonic() - self.record.started) * 1000.0

    def on_hangup(self) -> None:
        self._inject(Event("hangup"))

    def on_prompt_done(self) -> None:
        """Media finished playing the last clip → start the input timer now."""
        with self._lock:
            if self._timer_seconds is not None and not self.session.finished:
                self._arm_timer(self._timer_seconds)

    # ---- internals ------------------------------------------------------------------------

    _timer_seconds: Optional[float] = None

    def _nlu(self, text: str, today: date) -> NluOutcome:
        if self._pending_nlu is not None:
            out, self._pending_nlu = self._pending_nlu, None
            return out
        return default_nlu(text, today)

    def _on_nlu_done(self, resp: Response, text: str) -> None:
        if resp.ok and "outcome" in resp.payload:
            self._pending_nlu = resp.payload["outcome"]
        elif resp.ok and "intent" in resp.payload:
            # remote NLU service: the outcome object does not travel over JSON – rebuild it locally and
            # keep the remote verdict for the intent (it may come from the bge-m3 verifier)
            out = default_nlu(text, self.session.today)
            remote_intent = str(resp.payload["intent"])
            if remote_intent != out.intent.intent and resp.payload.get("decision") in ("operator", "execute"):
                out = NluOutcome(out.interpretation, replace(out.intent, intent=remote_intent, confidence=float(resp.payload.get("confidence", out.intent.confidence))))
            self._pending_nlu = out
        else:
            self.record.errors.append(f"nlu: {resp.status} {resp.payload.get('error', '')}")
        self._inject(Event("speech", text))

    def _inject(self, event: Event) -> None:
        with self._lock:
            if self.session.finished:
                return
            self._cancel_timer()
            self._generation += 1
            self._execute(self.session.handle(event))

    @property
    def listening(self) -> bool:
        """True while the current state has a ``listen`` armed (ASR audio is accepted only then)."""
        return self._listening and not self.session.finished

    def _execute(self, actions: list[Action]) -> None:
        self._timer_seconds = None
        self._listening = False
        for a in actions:
            if a.kind == "say":
                self.record.prompts.append(a.text)
                self._say(a.text, a.rate)
            elif a.kind == "stop_playback":
                self.media.stop(self.call_id)
            elif a.kind == "collect":
                self._collect = a.args
            elif a.kind == "listen":
                self._listening = True
                self._timer_seconds = max(self._timer_seconds or 0.0, float(a.args["seconds"]))
            elif a.kind == "timer":
                self._timer_seconds = max(self._timer_seconds or 0.0, float(a.args["seconds"]))
            elif a.kind == "call":
                self._call_module(a)
            elif a.kind == "sms":
                self.record.sms.append(a.args)
                self._fire_and_forget("sms", "send", {"template": a.args["template"], "vars": a.args["vars"]})
            elif a.kind == "transfer":
                self.record.transfer = a.args
                self._transfer(a.args)
            elif a.kind == "end":
                self._finish(a.args["status"])
        if self._timer_seconds is not None and not self.session.finished:
            # media will call on_prompt_done(); fall back to an estimate so a lost callback cannot hang the call
            est = sum(len(p) for p in self.record.prompts[-3:]) / self.speech_chars_per_s
            self._arm_timer(self._timer_seconds + min(est, 30.0))

    def _say(self, text: str, rate: float) -> None:
        if "tts" not in self.bus.modules():
            return
        try:
            fut = self.bus.submit("tts", "synth", {"text": text, "rate": rate, "call_id": self.call_id}, self.call_id, timeout=4.0)
        except Overloaded:
            self.record.errors.append("tts overloaded")
            return
        gen = self._generation

        def done(f) -> None:  # noqa: ANN001
            resp: Response = f.result()
            self.record.module_calls.append(resp)
            if resp.ok and gen == self._generation:
                clip = {"text": text, **resp.payload}
                self.record.clips.append(clip)
                self.media.play(self.call_id, clip)
            elif not resp.ok:
                self.record.errors.append(f"tts: {resp.status} {resp.payload.get('error', '')}")

        fut.add_done_callback(done)

    def _call_module(self, a: Action) -> None:
        module, op, args = a.args["module"], a.args["op"], a.args["args"]
        if module == "crm" and "call_id" not in args:  # stand records carry the call id (traceability of every write)
            args = {**args, "call_id": self.call_id}
        gen = self._generation
        try:
            fut = self.bus.submit(module, op, args, self.call_id, timeout=6.0)
        except Overloaded:
            self._inject(Event("module", "error", {"error": "overloaded"}, request_id=a.request_id))
            return

        def done(f) -> None:  # noqa: ANN001
            resp: Response = f.result()
            self.record.module_calls.append(resp)
            with self._lock:
                if gen != self._generation or self.session.finished:
                    return
                self._generation += 1
                self._execute(self.session.handle(Event("module", resp.status, resp.payload, request_id=a.request_id)))

        fut.add_done_callback(done)

    def _fire_and_forget(self, module: str, op: str, payload: dict) -> None:
        if module not in self.bus.modules():
            return
        try:
            fut = self.bus.submit(module, op, payload, self.call_id, timeout=30.0)
            fut.add_done_callback(lambda f: self.record.module_calls.append(f.result()))
        except Overloaded:
            self.record.errors.append(f"{module} overloaded")

    def _transfer(self, ticket_vars: dict) -> None:
        payload = {"call_id": self.call_id, "reason": ticket_vars.get("reason"), "vars": ticket_vars.get("vars", {}),
                   "history": ticket_vars.get("history", []), "utterances": list(self.record.utterances),
                   "user_state": self._user_state_payload()}
        if "operator" in self.bus.modules():
            fut = self.bus.submit("operator", "transfer", payload, self.call_id, timeout=10.0)

            def done(f) -> None:  # noqa: ANN001
                resp: Response = f.result()
                self.record.module_calls.append(resp)
                self.media.transfer(self.call_id, resp.payload if resp.ok else payload)

            fut.add_done_callback(done)
        else:
            self.media.transfer(self.call_id, payload)

    def _user_state_payload(self) -> Optional[dict]:
        """Latest early decision for the operator window: state label, cues, early intent, timing (no PII)."""
        if not self.record.early:
            return None
        last = self.record.early[-1]
        us = last.get("user_state", {})
        return {"label": us.get("label"), "cues": us.get("cues", []), "scores": us.get("scores", {}),
                "early_intent": last.get("intent"), "confidence": last.get("confidence"), "decided_at_ms": last.get("decided_at_ms"),
                "reason": last.get("reason")}

    def _finish(self, status: str) -> None:
        self._cancel_timer()
        self.record.status = status
        self.record.ended = time.monotonic()
        self.media.hangup(self.call_id, status)
        if self.on_finish:
            self.on_finish(self)

    def _arm_timer(self, seconds: float) -> None:
        self._cancel_timer()
        gen = self._generation

        def fire() -> None:
            with self._lock:
                if gen != self._generation or self.session.finished:
                    return
                self._generation += 1
                self._execute(self.session.handle(Event("timeout")))

        self._timer = threading.Timer(seconds, fire)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

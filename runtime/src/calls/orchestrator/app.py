"""Orchestrator facade: live call registry + HTTP-friendly ``handle(op, args)``.

Ops (used by the telephony bridge and by tests)::

    call.start   {call_id, flow, caller_phone?, vars?}  → {state, prompts}
    call.dtmf    {call_id, digit}
    call.speech  {call_id, text}
    call.silence {call_id}
    call.prompt_done {call_id, prompt_id?}
    call.audio   {call_id, pcm_b64, rate}  → PCM16 utterance for the ASR module (text comes back as a speech event)
    call.next_prompt {call_id}  → next synthesised clip to play ({} when the playback queue is empty)
    call.hangup  {call_id}
    call.state   {call_id} → full record (prompts, clips, status, transfer, sms)
    metrics      {}        → bus metrics + active calls
"""

from __future__ import annotations

import base64
import os
import threading
from collections import deque
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from calls.crm.registry import MockRegistry
from calls.scenario.dsl import Flow, load_flows, validate
from calls.scenario.engine import Event

from .modules import HttpModuleClient, build_stack
from .session import CallRunner, MediaSink

DEFAULT_CLINIC = {"clinic_name": "Городская поликлиника", "clinic_phone": "+7 (000) 000-00-00"}


class Orchestrator:
    def __init__(self, flows: dict[str, Flow], reg: MockRegistry, today: Callable[[], date] = date.today, clinic: Optional[dict] = None,
                 remote: Optional[dict[str, str]] = None, media: Optional[MediaSink] = None, max_calls: int = 200,
                 on_write: Optional[Callable[[str, dict], None]] = None, stand: Optional[object] = None) -> None:
        problems = validate(flows)
        if problems:
            raise ValueError("invalid scenarios: " + "; ".join(problems))
        self.flows = flows
        self.reg = reg
        self.today = today
        self.clinic = {**DEFAULT_CLINIC, **(clinic or {})}
        self.stand = stand  # StandSync when the in-process crm/operator modules write to the stand
        if on_write is None and stand is not None:
            on_write = stand.on_write  # type: ignore[attr-defined]
        self.stack = build_stack(reg, today, clinic=self.clinic, on_write=on_write)
        for name, url in (remote or {}).items():  # replace in-process module by a remote service
            self.stack.bus._modules[name].handler = HttpModuleClient(url).handle  # noqa: SLF001 – deliberate override
        self.bus = self.stack.bus.start()
        self._playback: dict[str, deque] = {}
        self._prompt_seq = 0
        self.media = media or MediaSink(play=self._queue_clip, stop=self._flush_clips)
        self.max_calls = max_calls
        self._calls: dict[str, CallRunner] = {}
        self._finished: dict[str, CallRunner] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, reg: MockRegistry) -> "Orchestrator":
        flows = load_flows(os.environ.get("SCENARIOS_DIR", "scenarios"))
        remote = {m: os.environ[f"{m.upper()}_URL"] for m in ("crm", "tts", "nlu", "sms", "operator", "asr") if os.environ.get(f"{m.upper()}_URL")}
        clinic = {"clinic_name": os.environ.get("CLINIC_NAME", DEFAULT_CLINIC["clinic_name"]), "clinic_phone": os.environ.get("CLINIC_PHONE", DEFAULT_CLINIC["clinic_phone"])}
        stand = None
        if "crm" not in remote or "operator" not in remote:  # in-process modules → this process owns the stand outbox
            from calls.crm.stand import StandSync

            stand = StandSync.from_env(reg)
        frozen = os.environ.get("CRM_NOW", "").strip()  # deterministic clock for demos / compose e2e
        today: Callable[[], date] = (lambda d=date.fromisoformat(frozen[:10]): d) if frozen else date.today
        return cls(flows, reg, today=today, clinic=clinic, remote=remote, stand=stand)

    # ---- call management -------------------------------------------------------------------

    def start_call(self, call_id: str, flow: str = "inbound", caller_phone: str = "", vars: Optional[dict] = None) -> CallRunner:
        with self._lock:
            if call_id in self._calls:
                raise ValueError(f"call {call_id} already active")
            if len(self._calls) >= self.max_calls:
                raise RuntimeError("too many active calls")
            v = {"caller_phone": caller_phone, "today": self.today().isoformat(), **self.clinic, **(vars or {})}
            runner = CallRunner(call_id, self.flows, flow, self.bus, vars=v, today=self.today(), media=self.media, on_finish=self._on_finish)
            self._calls[call_id] = runner
        runner.start()
        return runner

    # ---- playback queue (pull model for the telephony bridge) ---------------------------------

    def _queue_clip(self, call_id: str, clip: dict) -> None:
        with self._lock:
            self._prompt_seq += 1
            self._playback.setdefault(call_id, deque()).append({"prompt_id": self._prompt_seq, **clip})

    def _flush_clips(self, call_id: str) -> None:
        with self._lock:
            self._playback.pop(call_id, None)

    def next_prompt(self, call_id: str) -> dict:
        with self._lock:
            q = self._playback.get(call_id)
            if not q:
                return {}
            clip = q.popleft()
        pcm = clip.get("pcm")
        out = {k: v for k, v in clip.items() if k != "pcm"}
        if isinstance(pcm, (bytes, bytearray)):
            out["pcm_b64"] = base64.b64encode(pcm).decode()
        return out

    def _on_finish(self, runner: CallRunner) -> None:
        with self._lock:
            self._calls.pop(runner.call_id, None)
            self._finished[runner.call_id] = runner
            if len(self._finished) > 1000:
                for k in list(self._finished)[:500]:
                    del self._finished[k]

    def get(self, call_id: str) -> Optional[CallRunner]:
        with self._lock:
            return self._calls.get(call_id) or self._finished.get(call_id)

    def active(self) -> int:
        with self._lock:
            return len(self._calls)

    # ---- HTTP-style handler -----------------------------------------------------------------

    def handle(self, op: str, args: dict) -> tuple[str, dict]:
        if op == "metrics":
            return "ok", {"active_calls": self.active(), "modules": self.bus.metrics(), "tts_cache": {"hits": self.stack.tts.hits, "misses": self.stack.tts.misses},
                          "stand": self.stand.status() if self.stand is not None else None}  # type: ignore[attr-defined]
        if op == "stand.status":
            return "ok", self.stand.status() if self.stand is not None else {"mode": "off", "mirror": self.reg.sync_report()}  # type: ignore[attr-defined]
        if op == "stand.flush":  # wait until the outbox is empty (test-case runner / e2e)
            ok = self.stand.join(float(args.get("timeout", 30))) if self.stand is not None else True  # type: ignore[attr-defined]
            return ("ok" if ok else "timeout"), (self.stand.status() if self.stand is not None else {"mode": "off"})  # type: ignore[attr-defined]
        call_id = str(args.get("call_id", ""))
        if op == "call.start":
            try:
                r = self.start_call(call_id, args.get("flow", "inbound"), args.get("caller_phone", ""), args.get("vars"))
            except (ValueError, RuntimeError, KeyError) as exc:
                return "error", {"error": str(exc)}
            return "ok", {"state": r.session.state_name, "prompts": list(r.record.prompts)}
        r = self.get(call_id)
        if r is None:
            return "error", {"error": f"unknown call {call_id}"}
        if op == "call.dtmf":
            r.on_dtmf(str(args["digit"]))
        elif op == "call.speech":
            r.on_speech(str(args.get("text", "")))
        elif op == "call.silence":
            r.on_silence()
        elif op == "call.prompt_done":
            r.on_prompt_done()
        elif op == "call.next_prompt":
            return "ok", self.next_prompt(call_id)
        elif op == "call.audio":
            self._transcribe(r, args)
        elif op == "call.partial":
            r.on_partial(str(args.get("text", "")), args.get("at_ms"), str(args.get("stable_text", "")))
        elif op == "call.timeout":  # test hook: fire the pending no-input timer now (case runner against a deployed stack)
            r._inject(Event("timeout"))  # noqa: SLF001
        elif op == "call.hangup":
            r.on_hangup()
            self._flush_clips(call_id)
        elif op != "call.state":
            return "error", {"error": f"unknown op {op}"}
        rec = r.record
        return "ok", {
            "state": r.session.state_name, "finished": r.finished, "status": rec.status, "prompts": list(rec.prompts),
            "clips": [{k: v for k, v in c.items() if k != "pcm"} for c in rec.clips], "transfer": rec.transfer, "sms": rec.sms,
            "vars": {k: v for k, v in r.session.vars.items() if k not in ("snils", "card_no", "birth_date", "phone", "caller_phone")},
            "errors": rec.errors, "duration_s": round(rec.duration_s, 2),
            "early": rec.early[-1] if rec.early else None, "user_state": rec.user_state,
        }

    def _transcribe(self, r: CallRunner, args: dict) -> None:
        """Hand an utterance to the ASR module; its text re-enters the call as a speech event."""
        if "asr" not in self.bus.modules():
            r.record.errors.append("asr: no module")
            return
        if not r.listening:
            return  # DTMF phase: audio is not sent to the recogniser (policy: ASR only after the 60 s without keypress)
        payload = {"pcm_b64": args.get("pcm_b64", ""), "rate": int(args.get("rate", 8000)), "text": args.get("text", "")}
        if payload["pcm_b64"]:
            try:
                r.on_audio(base64.b64decode(payload["pcm_b64"]))  # prosody for the caller state (cheap, in-process)
            except (ValueError, TypeError):
                pass
        fut = self.bus.submit("asr", "transcribe", payload, r.call_id, timeout=8.0)

        def done(f) -> None:  # noqa: ANN001
            resp = f.result()
            r.record.module_calls.append(resp)
            text = str(resp.payload.get("text", "")).strip() if resp.ok else ""
            if text and not r.finished:
                r.on_speech(text)
            elif not resp.ok:
                r.record.errors.append(f"asr: {resp.status} {resp.payload.get('error', '')}")

        fut.add_done_callback(done)

    def shutdown(self) -> None:
        self.bus.stop()


def load_default_flows() -> dict[str, Flow]:
    here = Path(__file__).resolve().parents[3] / "scenarios"
    return load_flows(here if here.exists() else "scenarios")

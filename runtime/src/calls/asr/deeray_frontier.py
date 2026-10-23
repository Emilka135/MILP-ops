"""Frontier tracker for the GigaAM v3 Runtime (Deeray) ``/v1/stream`` protocol.

Runtime messages (``emit=words``):

* ``{"type":"words","at":t,"revise_from":i,"words":[{start,end,text,stable}]}`` —
  replace the hypothesis tail from word index ``i`` (the reference Python client does
  ``words[revise_from:] = new_words``).
* ``{"type":"stable","at":t,"upto":u}`` — words before ``u`` will not be revised.
* ``{"type":"final","at":t,"upto":u,"endpoint":e}`` — words before ``u`` are immutable;
  ``e`` is the engine's end-of-utterance time.
* ``{"type":"turns",...}`` (ignored here), ``{"type":"error","message":m}``, ``{"type":"end"}``.

ASSUMPTION (to verify against the pinned runtime version): ``upto`` is a word *index*
(exclusive), consistent with ``revise_from``. If the runtime sends a float, this tracker
treats it as a media *time* and converts it to the index of the last word ending at or
before that time. Either way the dialog layer sees the same normalised events.

The tracker also audits the runtime's monotonicity guarantees (stable frontier never
moves backwards, no revision below the stable/final frontier); each breach is recorded
as a :class:`ProtocolViolation` and — because a telephony dialog must keep going — the
message is still applied with the frontier clamped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import AsrEvent, Word


@dataclass(frozen=True)
class ProtocolViolation:
    at: float
    kind: str
    detail: str


@dataclass
class DeerayStreamTracker:
    words: list[Word] = field(default_factory=list)
    stable_upto: int = 0
    final_upto: int = 0
    violations: list[ProtocolViolation] = field(default_factory=list)
    last_at: float = 0.0
    closed: bool = False

    # ------------------------------------------------------------------ helpers
    def _text(self, lo: int, hi: int) -> str:
        return " ".join(w.text for w in self.words[lo:hi]).strip()

    def _violation(self, at: float, kind: str, detail: str) -> None:
        self.violations.append(ProtocolViolation(at, kind, detail))

    def _upto_index(self, raw, at: float) -> int:
        if isinstance(raw, bool):
            raise ValueError("upto must be a number")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float):
            if raw.is_integer() and raw <= len(self.words):
                return int(raw)
            # time semantics: count words that end at or before ``raw``
            n = 0
            for w in self.words:
                if w.end <= raw + 1e-9:
                    n += 1
                else:
                    break
            return n
        raise ValueError(f"unsupported upto value {raw!r}")

    # ------------------------------------------------------------------ main
    def on_message(self, msg: dict) -> list[AsrEvent]:
        kind = msg.get("type")
        at = float(msg.get("at", self.last_at))
        if at + 1e-9 < self.last_at:
            self._violation(at, "time", f"at went backwards: {at} < {self.last_at}")
        self.last_at = max(self.last_at, at)
        if self.closed and kind not in ("end", "error"):
            self._violation(at, "closed", f"{kind} after end")

        if kind == "words":
            return self._on_words(msg, at)
        if kind == "stable":
            return self._on_stable(msg, at)
        if kind == "final":
            return self._on_final(msg, at)
        if kind == "turns":
            return []
        if kind == "error":
            return [AsrEvent("error", at, message=str(msg.get("message", "")))]
        if kind == "end":
            return self._on_end(at)
        self._violation(at, "unknown", f"unknown message type {kind!r}")
        return []

    def _on_words(self, msg: dict, at: float) -> list[AsrEvent]:
        rf = int(msg.get("revise_from", 0))
        new = [Word.from_json(w) for w in msg.get("words", [])]
        if rf < 0:
            self._violation(at, "revise_from", f"negative revise_from {rf}")
            rf = 0
        if rf > len(self.words):
            self._violation(at, "revise_from", f"revise_from {rf} beyond hypothesis length {len(self.words)}")
            rf = len(self.words)
        if rf < self.final_upto:
            self._violation(at, "final", f"revision at {rf} below final frontier {self.final_upto}")
            drop = self.final_upto - rf
            new = new[drop:]
            rf = self.final_upto
        if rf < self.stable_upto:
            old = self._text(rf, self.stable_upto)
            fresh = " ".join(w.text for w in new[: self.stable_upto - rf]).strip()
            if old != fresh:
                self._violation(at, "stable", f"stable text changed: {old!r} -> {fresh!r}")
        self.words[rf:] = new
        if self.stable_upto > len(self.words):
            self.stable_upto = len(self.words)
        return [
            AsrEvent(
                "partial",
                at,
                text=self._text(self.final_upto, len(self.words)),
                words=tuple(self.words[self.final_upto :]),
            )
        ]

    def _on_stable(self, msg: dict, at: float) -> list[AsrEvent]:
        upto = self._upto_index(msg.get("upto", self.stable_upto), at)
        if upto > len(self.words):
            self._violation(at, "stable", f"stable upto {upto} beyond hypothesis length {len(self.words)}")
            upto = len(self.words)
        if upto < self.stable_upto:
            self._violation(at, "stable", f"stable frontier moved backwards {self.stable_upto} -> {upto}")
            upto = self.stable_upto
        self.stable_upto = upto
        return [
            AsrEvent(
                "stable",
                at,
                text=self._text(self.final_upto, self.stable_upto),
                words=tuple(self.words[self.final_upto : self.stable_upto]),
            )
        ]

    def _on_final(self, msg: dict, at: float) -> list[AsrEvent]:
        upto = self._upto_index(msg.get("upto", len(self.words)), at)
        endpoint = msg.get("endpoint")
        if upto > len(self.words):
            self._violation(at, "final", f"final upto {upto} beyond hypothesis length {len(self.words)}")
            upto = len(self.words)
        if upto < self.final_upto:
            self._violation(at, "final", f"final frontier moved backwards {self.final_upto} -> {upto}")
            upto = self.final_upto
        lo = self.final_upto
        self.final_upto = upto
        self.stable_upto = max(self.stable_upto, upto)
        return [
            AsrEvent(
                "final",
                at,
                text=self._text(lo, upto),
                words=tuple(self.words[lo:upto]),
                endpoint=float(endpoint) if endpoint is not None else None,
            )
        ]

    def _on_end(self, at: float) -> list[AsrEvent]:
        out: list[AsrEvent] = []
        if self.final_upto < len(self.words):
            lo = self.final_upto
            self.final_upto = len(self.words)
            self.stable_upto = len(self.words)
            out.append(
                AsrEvent("final", at, text=self._text(lo, len(self.words)), words=tuple(self.words[lo:]), flushed=True)
            )
        self.closed = True
        out.append(AsrEvent("end", at))
        return out

    # ------------------------------------------------------------------ views
    @property
    def open_text(self) -> str:
        return self._text(self.final_upto, len(self.words))

    @property
    def stable_text(self) -> str:
        return self._text(self.final_upto, self.stable_upto)

    @property
    def committed_text(self) -> str:
        return self._text(0, self.final_upto)

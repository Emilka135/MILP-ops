"""Engine-neutral streaming ASR events (docs/ARCHITECTURE.md §S5, contract C-3).

Invariants the dialog layer may rely on:

* ``partial`` — the full current hypothesis of the *open* utterance; may change arbitrarily.
* ``stable``  — the prefix of the open utterance that the engine promises not to revise;
  the prefix only grows (monotone) until the next ``final``.
* ``final``   — the open utterance is closed; its text is immutable from now on. Carries
  ``endpoint`` (media time of the end of speech as seen by the engine) so that end-to-end
  latency is measured as ``at − endpoint``.
* ``error`` / ``end`` — session-level.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str
    stable: bool = False

    @classmethod
    def from_json(cls, d: dict) -> Word:
        return cls(float(d["start"]), float(d["end"]), str(d["text"]), bool(d.get("stable", False)))


@dataclass(frozen=True)
class AsrEvent:
    kind: str  # "partial" | "stable" | "final" | "error" | "end"
    at: float
    text: str = ""
    words: tuple[Word, ...] = ()
    endpoint: float | None = None
    message: str = ""
    flushed: bool = False  # final produced by stream end rather than by the engine

    @property
    def latency(self) -> float | None:
        """Engine-side commit latency (seconds) for ``final`` events with an endpoint."""
        if self.kind == "final" and self.endpoint is not None:
            return self.at - self.endpoint
        return None

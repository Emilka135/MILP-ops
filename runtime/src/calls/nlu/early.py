"""Early intent + caller state from streaming ASR partials and prosody — decided within the first seconds.

**Provenance.**  The "intent and caller state in under three seconds" idea is attributed in the brief to
Deeray / Krylov (2026).  No public description of that method exists (see docs/RESEARCH_ASR_2026.md), so
this module is *our own design* with the same goal.  Nothing here is a reimplementation of a Deeray
component; the only Deeray artefact we rely on is the streaming event contract (``words``/``stable``/``final``).

**Idea.**  Streaming ASR gives a *partial* hypothesis every 200–300 ms.  Instead of waiting for the end of
the utterance (+600 ms end-pointing, +NLU), the tracker accumulates evidence per intent after every partial
and stops as soon as the evidence is conclusive — an *anytime* decision in the spirit of Wald's sequential
probability ratio test (SPRT): keep sampling while the log-odds stay inside the band, decide when they
leave it, and always decide at the deadline (default 3 s after speech onset).  Words the engine has marked
*stable* count fully; the volatile tail counts less because it may still be revised.

Two outputs:

* :class:`EarlyDecision` — ``intent`` with ``confidence`` and ``decided_at_ms``; ``escalate`` is set when a
  human-only intent (emergency / complaint / medical / operator) is already certain – the dialog layer may
  transfer *before* the caller has finished the sentence.
* :class:`UserState` — ``calm`` / ``hurried`` / ``agitated`` / ``distressed`` / ``confused``.  Features:
  speech rate (words/s), loudness and its variability relative to the caller's own first 400 ms (so a quiet
  line and a loud line are comparable), pitch variability (F0 by normalised autocorrelation on 8 kHz voiced
  frames), restarts (how often the partial retracted words), and lexical cues (pain, complaint, confusion
  markers, fillers).  Linear scores with documented weights → argmax with a prior for ``calm``.
  Agitated / distressed callers (pain, complaint) are routed to the operator with the state in the ticket.

Everything is deterministic and runs in microseconds per partial (NumPy only for the audio features), so it
sits in the per-call actor without a model server.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np

from .intent import HUMAN_ONLY, INTENTS, score_intents

# ---- caller-state lexicon -------------------------------------------------------------------------------------

_PAIN = re.compile(r"\bбол(ит|ь|ьно|и\b)|\bплохо\b|\bскор(ая|ую)\b|\bзадыха|\bкров(ь|отеч)|\bтемператур|\bприступ|\bсознани|\bумира|\bнечем дышать|\bтошнит|\bдавлени")
_ANGER = re.compile(r"\bжалоб|\bбезобрази|\bхам|\bвозмущ|\bнедовол|\bнагруби|\bсколько можно\b|\bопять\b|\bникто\b|\bникогда\b|\bчёрт|\bчерт\b|\bиздева|\bбардак|\bнемедленно\b")
_HURRY = re.compile(r"\bсрочно\b|\bбыстр(о|ее)\b|\bнекогда\b|\bопаздыва|\bскорее\b|\bпобыстрее\b|\bдавайте уже\b")
_CONFUSED = re.compile(r"\bне понима|\bне понял|\bне поняла|\bалло\b|\bчто\?|\bповтори|\bкуда я попал|\bэто поликлиника\b|\bа\?|\bчего\b|\bне слышно|\bкак это\b")
_FILLERS = re.compile(r"\bэ+м*\b|\bну\b|\bэто самое\b|\bкороче\b|\bкак бы\b|\bзначит\b|\bмм+\b")

STATES = ("calm", "hurried", "agitated", "distressed", "confused")


@dataclass
class ProsodyFeatures:
    speech_ms: float = 0.0  # voiced duration seen so far
    frames: int = 0
    loudness_db: float = 0.0  # mean dBFS of voiced frames
    loudness_delta_db: float = 0.0  # mean of the last part vs. the caller's first 400 ms (positive = getting louder)
    loudness_var_db: float = 0.0  # std of voiced-frame dBFS
    f0_hz: float = 0.0  # median F0 of voiced frames (0 when not estimable)
    f0_var: float = 0.0  # relative F0 variability (IQR / median)
    pause_ratio: float = 0.0  # unvoiced time inside speech / total


class ProsodyTracker:
    """Per-call audio features on 20 ms frames of 8 kHz PCM16.  Cheap: energy, ZCR, autocorrelation pitch."""

    FRAME = 160  # 20 ms at 8 kHz

    def __init__(self, rate: int = 8000, noise_floor_db: float = -50.0) -> None:
        self.rate = rate
        self.frame = int(rate * 0.02)
        self.noise_floor_db = noise_floor_db
        self._levels: list[float] = []  # dBFS of voiced frames
        self._f0: list[float] = []
        self._voiced_flags: list[bool] = []
        self._carry = np.zeros(0, dtype=np.float32)
        self._in_speech = False

    def feed(self, pcm: bytes | np.ndarray) -> None:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0 if isinstance(pcm, (bytes, bytearray)) else np.asarray(pcm, dtype=np.float32)
        if x.size and np.abs(x).max() > 1.5:  # int16 passed as array
            x = x / 32768.0
        x = np.concatenate([self._carry, x])
        n = (x.size // self.frame) * self.frame
        frames, self._carry = x[:n].reshape(-1, self.frame), x[n:]
        if not frames.size:
            return
        rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
        db = 20 * np.log10(rms)
        zcr = np.mean(np.abs(np.diff(np.sign(frames), axis=1)) > 0, axis=1)
        voiced = (db > self.noise_floor_db + 12) & (zcr < 0.35)
        for i in range(frames.shape[0]):
            self._voiced_flags.append(bool(voiced[i]))
            if voiced[i]:
                self._levels.append(float(db[i]))
                f0 = self._pitch(frames[i])
                if f0:
                    self._f0.append(f0)

    def _pitch(self, frame: np.ndarray) -> float:
        """Normalised autocorrelation peak in 70–400 Hz; 0 when the frame is not periodic enough."""
        f = frame - frame.mean()
        e = float(np.dot(f, f))
        if e < 1e-6:
            return 0.0
        lo, hi = int(self.rate / 400), int(self.rate / 70)
        if hi >= f.size:
            return 0.0
        best, best_lag = 0.0, 0
        for lag in range(lo, hi):
            r = float(np.dot(f[:-lag], f[lag:])) / e
            if r > best:
                best, best_lag = r, lag
        return self.rate / best_lag if best > 0.45 and best_lag else 0.0

    def features(self) -> ProsodyFeatures:
        flags = self._voiced_flags
        lv = np.asarray(self._levels, dtype=np.float32)
        feats = ProsodyFeatures(frames=len(flags))
        if not flags:
            return feats
        voiced_n = int(sum(flags))
        feats.speech_ms = voiced_n * 20.0
        # pauses inside speech: unvoiced frames between the first and the last voiced frame
        try:
            first = flags.index(True)
            last = len(flags) - 1 - flags[::-1].index(True)
            inside = flags[first : last + 1]
            feats.pause_ratio = round(1 - sum(inside) / max(1, len(inside)), 3)
        except ValueError:
            pass
        if lv.size:
            feats.loudness_db = round(float(lv.mean()), 1)
            feats.loudness_var_db = round(float(lv.std()), 1)
            head = lv[: max(1, min(20, lv.size // 3))]  # ≈ first 400 ms of voiced speech
            tail = lv[-max(1, min(25, lv.size // 2)) :]
            feats.loudness_delta_db = round(float(tail.mean() - head.mean()), 1)
        if len(self._f0) >= 5:
            f0 = np.asarray(self._f0)
            med = float(np.median(f0))
            q1, q3 = np.percentile(f0, [25, 75])
            feats.f0_hz = round(med, 1)
            feats.f0_var = round(float((q3 - q1) / med), 3) if med else 0.0
        return feats


@dataclass
class UserState:
    label: str
    scores: dict[str, float]
    features: dict[str, float]
    cues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def estimate_user_state(text: str, prosody: ProsodyFeatures | None, words_per_s: float, restarts: float) -> UserState:
    """Linear scores per state; weights are documented here and only here.

    * hurried:    speech rate > 3.2 w/s, hurry words, short pauses
    * agitated:   louder than own baseline (+4 dB), loud variability, high pitch variability, anger words, fast
    * distressed: pain words, high pitch variability with *low* rate, long pauses, loudness drop
    * confused:   confusion markers, fillers, restarts (retracted words), many pauses with low rate
    * calm:       prior 1.0 minus the others' evidence
    """
    t = " " + text.lower().replace("ё", "е") + " "
    p = prosody or ProsodyFeatures()
    cues: list[str] = []
    pain = len(_PAIN.findall(t))
    anger = len(_ANGER.findall(t))
    hurry = len(_HURRY.findall(t))
    confused = len(_CONFUSED.findall(t))
    fillers = len(_FILLERS.findall(t))
    for name, n in (("pain", pain), ("anger", anger), ("hurry", hurry), ("confusion", confused), ("fillers", fillers)):
        if n:
            cues.append(f"{name}×{n}")
    fast = max(0.0, words_per_s - 3.2)  # words/s above a brisk conversational rate
    slow = max(0.0, 1.6 - words_per_s) if words_per_s > 0 else 0.0
    louder = max(0.0, p.loudness_delta_db - 4.0) / 4.0
    quieter = max(0.0, -4.0 - p.loudness_delta_db) / 4.0
    loud_var = max(0.0, p.loudness_var_db - 6.0) / 4.0
    pitch_var = max(0.0, p.f0_var - 0.25) / 0.25
    pauses = max(0.0, p.pause_ratio - 0.35) / 0.3
    if p.speech_ms:
        for name, v in (("louder", louder), ("quieter", quieter), ("loud_var", loud_var), ("pitch_var", pitch_var), ("pauses", pauses)):
            if v > 0:
                cues.append(f"{name}={v:.2f}")
    if fast > 0:
        cues.append(f"fast={words_per_s:.1f}w/s")
    if restarts > 0.2:
        cues.append(f"restarts={restarts:.2f}")
    scores = {
        "hurried": 0.9 * fast + 1.2 * min(hurry, 2) - 0.6 * pauses,
        "agitated": 1.0 * louder + 0.8 * loud_var + 0.6 * pitch_var + 1.5 * min(anger, 2) + 0.4 * fast,
        "distressed": 1.6 * min(pain, 2) + 0.7 * pitch_var + 0.5 * pauses + 0.5 * quieter + 0.3 * slow,
        "confused": 1.3 * min(confused, 2) + 0.6 * min(fillers, 3) + 1.5 * min(restarts, 1.0) + 0.4 * pauses + 0.3 * slow,
    }
    scores["calm"] = 1.0 - 0.5 * sum(scores.values())
    label = max(STATES, key=lambda s: (scores[s], s == "calm"))
    if scores[label] < 1.0:
        label = "calm"
    feats = {"words_per_s": round(words_per_s, 2), "restarts": round(restarts, 3), **{k: v for k, v in asdict(p).items() if k != "frames"}}
    return UserState(label, {k: round(v, 2) for k, v in scores.items()}, feats, cues)


# ---- anytime intent decision ---------------------------------------------------------------------------------------


@dataclass
class EarlyDecision:
    intent: str
    confidence: float
    decided_at_ms: float
    reason: str  # "evidence" | "deadline" | "final" | "escalate"
    escalate: bool
    user_state: UserState
    text: str
    posteriors: dict[str, float]

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


class EarlyIntentTracker:
    """Feed partials (and optionally audio); read :meth:`decision` – ``None`` until the tracker has decided.

    Stopping rule (sequential test on the rule evidence, same scoring as :func:`calls.nlu.intent.classify`
    so the early verdict and the full NLU agree): decide as soon as the best intent has evidence
    ≥ ``min_evidence`` (the classifier's own threshold), its confidence – ``0.55 + 0.1·best − 0.05·second``,
    exactly the classifier's formula – is ≥ ``p_decide``, the lead over the runner-up is ≥ ``margin``, and the
    same intent has led for ``hold`` consecutive partials (hysteresis against flicker).  ``deadline_s`` after
    speech onset the best guess is emitted whatever the evidence.
    """

    def __init__(
        self,
        deadline_s: float = 3.0,
        p_decide: float = 0.7,
        margin: float = 0.5,
        hold: int = 2,
        min_evidence: float = 1.5,
        temperature: float = 1.0,
        volatile_weight: float = 0.6,
        escalate_evidence: float = 2.5,
        prosody: ProsodyTracker | None = None,
    ) -> None:
        self.deadline_ms = deadline_s * 1000.0
        self.p_decide = p_decide
        self.margin = margin
        self.hold = hold
        self.min_evidence = min_evidence
        self.temperature = temperature
        self.volatile_weight = volatile_weight
        self.escalate_evidence = escalate_evidence
        self.prosody = prosody
        self.onset_ms: Optional[float] = None
        self.partials: list[tuple[float, str]] = []
        self.retracted_words = 0
        self.total_words = 0
        self._streak: tuple[str, int] = ("", 0)
        self._decision: Optional[EarlyDecision] = None
        self.evidence: Counter[str] = Counter()
        self._last_text = ""
        self._prev_text = ""

    # ---- inputs --------------------------------------------------------------------------------------------------

    def on_audio(self, pcm: bytes | np.ndarray) -> None:
        if self.prosody is None:
            self.prosody = ProsodyTracker()
        self.prosody.feed(pcm)

    def on_partial(self, at_ms: float, text: str, stable_text: str = "") -> Optional[EarlyDecision]:
        """``text`` is the full current hypothesis; ``stable_text`` the prefix the engine will not revise."""
        text = text.strip()
        if self.onset_ms is None and text:
            self.onset_ms = at_ms
        if not text:
            return self._maybe_deadline(at_ms)
        # restarts: words of the previous partial that disappeared from this one
        prev = self._last_text.split()
        cur = text.split()
        common = 0
        for a, b in zip(prev, cur):
            if a == b:
                common += 1
            else:
                break
        grown = common < len(prev) and common < len(cur) and cur[common].startswith(prev[common])  # "запи" → "записаться"
        self.retracted_words += max(0, len(prev) - common - (1 if grown else 0))
        self.total_words = max(self.total_words, len(cur))
        self._prev_text, self._last_text = self._last_text, text
        self.partials.append((at_ms, text))
        # stability: what the engine promised not to revise, or – lacking that – the word prefix that survived
        # the previous partial unchanged (the usual trick when the engine emits plain partials only)
        stable_text = stable_text.strip()
        survived = " ".join(cur[:common])
        if len(survived) > len(stable_text):
            stable_text = survived
        volatile = text[len(stable_text) :].strip() if stable_text and text.startswith(stable_text) else text
        ev: Counter[str] = Counter()
        for k, v in score_intents(stable_text).items():
            ev[k] += v
        for k, v in score_intents(volatile).items():
            ev[k] += v * self.volatile_weight
        self.evidence = ev  # evidence is recomputed from the current hypothesis (partials supersede each other)
        return self._evaluate(at_ms, final=False)

    def tick(self, at_ms: float) -> Optional[EarlyDecision]:
        """Clock without new text (call it from a timer): forces the deadline decision."""
        return self._maybe_deadline(at_ms)

    def on_final(self, at_ms: float, text: str) -> EarlyDecision:
        """End of utterance: always yields a decision (even when evidence is weak → ``unknown``)."""
        text = text.strip()
        if text:
            self._last_text = text
            self.partials.append((at_ms, text))
            self.total_words = max(self.total_words, len(text.split()))
            self.evidence = Counter(score_intents(text))
        d = self._evaluate(at_ms, final=True)
        assert d is not None
        return d

    # ---- outputs -------------------------------------------------------------------------------------------------

    @property
    def decision(self) -> Optional[EarlyDecision]:
        return self._decision

    @property
    def decided(self) -> bool:
        return self._decision is not None

    def posteriors(self) -> dict[str, float]:
        """Softmax over the intents that have evidence plus an ``unknown`` hypothesis fixed at evidence 1.0 –
        a single weak cue (score ≈ 1) therefore never wins, two consistent cues or one strong keyword do."""
        logits = {k: v / self.temperature for k, v in self.evidence.items() if v > 0 and k in INTENTS}
        logits["unknown"] = 1.0 / self.temperature
        m = max(logits.values())
        z = {k: math.exp(v - m) for k, v in logits.items()}
        s = sum(z.values())
        return {k: v / s for k, v in z.items()}

    def words_per_s(self, at_ms: float) -> float:
        """Words per second since onset; 0 (= unknown) during the first second, when partials arrive in bursts."""
        if self.onset_ms is None:
            return 0.0
        span = (at_ms - self.onset_ms) / 1000.0
        return self.total_words / span if span >= 1.0 else 0.0

    def restarts(self) -> float:
        return min(1.0, self.retracted_words / max(1, self.total_words))

    def user_state(self, at_ms: float) -> UserState:
        feats = self.prosody.features() if self.prosody is not None else None
        return estimate_user_state(self._last_text, feats, self.words_per_s(at_ms), self.restarts())

    # ---- internals -----------------------------------------------------------------------------------------------

    def _maybe_deadline(self, at_ms: float) -> Optional[EarlyDecision]:
        if self._decision is None and self.onset_ms is not None and at_ms - self.onset_ms >= self.deadline_ms:
            return self._evaluate(at_ms, final=False, force=True)
        return self._decision

    def ranking(self) -> tuple[str, float, float, float]:
        """→ (best intent, best evidence, runner-up evidence, confidence) with the classifier's confidence formula."""
        ev = {k: v for k, v in self.evidence.items() if k in INTENTS and v > 0}
        if not ev:
            return "unknown", 0.0, 0.0, 0.0
        best = max(ev.values())
        intent = next(i for i in INTENTS if ev.get(i, 0.0) == best)  # INTENTS order breaks ties (safety first)
        second = max((v for k, v in ev.items() if k != intent), default=0.0)
        if best < self.min_evidence:
            return "unknown", best, second, round(best / (self.min_evidence * 2), 2)
        conf = min(0.97, 0.55 + 0.1 * best - 0.05 * second)
        return intent, best, second, round(max(conf, 0.5), 2)

    def _evaluate(self, at_ms: float, final: bool, force: bool = False) -> Optional[EarlyDecision]:
        if self._decision is not None:
            return self._decision
        post = self.posteriors()
        intent, best, second, conf = self.ranking()
        state = self.user_state(at_ms)
        # 1. a human-only intent with strong evidence → escalate right now (pain, complaint, "оператора!")
        unsafe = max(((k, v) for k, v in self.evidence.items() if k in HUMAN_ONLY), key=lambda kv: kv[1], default=None)
        if unsafe and unsafe[1] >= self.escalate_evidence:
            return self._decide(unsafe[0], max(conf, 0.9), post, at_ms, "escalate", True, state)
        # 1b. the caller's state alone is a reason to hand over: distressed/agitated with a pain or anger cue
        if state.label in ("distressed", "agitated") and any(c.startswith(("pain", "anger")) for c in state.cues):
            fallback = "emergency" if state.label == "distressed" else "complaint"
            return self._decide(intent if intent != "unknown" else fallback, max(conf, 0.9), post, at_ms, "escalate", True, state)
        # 2. sequential test: enough evidence, confident, clear lead, held for `hold` partials
        if intent != "unknown" and conf >= self.p_decide and best - second >= self.margin:
            name, n = self._streak
            self._streak = (intent, n + 1) if name == intent else (intent, 1)
            if self._streak[1] >= self.hold or final:
                return self._decide(intent, conf, post, at_ms, "final" if final else "evidence", intent in HUMAN_ONLY, state)
        else:
            self._streak = ("", 0)
        # 3. end of utterance / deadline: best guess with the classifier's confidence (``unknown`` when weak)
        if final:
            return self._decide(intent, conf, post, at_ms, "final", intent in HUMAN_ONLY, state)
        if force or (self.onset_ms is not None and at_ms - self.onset_ms >= self.deadline_ms):
            return self._decide(intent, conf, post, at_ms, "deadline", intent in HUMAN_ONLY, state)
        return None

    def on_asr_event(self, ev: "AsrEvent") -> Optional[EarlyDecision]:  # noqa: F821 - imported lazily (no cycle)
        """Adapter for :class:`calls.asr.events.AsrEvent` (Deeray ``words``/``stable``/``final`` stream)."""
        at_ms = ev.at * 1000.0
        if ev.kind in ("partial", "stable"):
            stable = " ".join(w.text for w in ev.words if w.stable) if ev.words else (ev.text if ev.kind == "stable" else "")
            text = ev.text or " ".join(w.text for w in ev.words)
            return self.on_partial(at_ms, text, stable)
        if ev.kind == "final":
            return self.on_final(at_ms, ev.text or " ".join(w.text for w in ev.words))
        return self.tick(at_ms)

    def _decide(self, intent: str, conf: float, post: dict[str, float], at_ms: float, reason: str, escalate: bool, state: UserState) -> EarlyDecision:
        self._decision = EarlyDecision(
            intent=intent,
            confidence=round(conf, 3),
            decided_at_ms=round(at_ms - (self.onset_ms if self.onset_ms is not None else at_ms), 1),
            reason=reason,
            escalate=escalate,
            user_state=state,
            text=self._last_text,
            posteriors={k: round(v, 3) for k, v in post.items() if v >= 0.01},
        )
        return self._decision

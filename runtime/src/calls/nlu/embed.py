"""Embedding mini-agent: nearest-example intent classification on CPU.

Production embedder: ``deepvk/USER-bge-m3`` (Apache-2.0, 359M params, 1024-d,
~1.4 GB fp32 / ~0.7 GB fp16 on CPU, ≈30–60 ms per short utterance on 4 cores).
It is loaded lazily and only if ``sentence_transformers`` is installed; the
test-suite and the offline sandbox use ``HashEmbedder`` – a deterministic
character-n-gram hashing embedder with the same interface, so the pipeline
(normalise → rules → embeddings → verify → scenario) is testable without models.
"""

from __future__ import annotations

import logging

import hashlib
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

import numpy as np

from .normalize import canonicalize


class Embedder(Protocol):
    name: str

    def encode(self, texts: Sequence[str]) -> np.ndarray:  # (n, d), L2-normalised rows
        ...


class HashEmbedder:
    """Char n-gram (3–5) hashing into ``dim`` buckets, L2-normalised. Deterministic."""

    name = "hash-ngram"

    def __init__(self, dim: int = 1024, ngrams: tuple[int, ...] = (3, 4, 5)) -> None:
        self.dim = dim
        self.ngrams = ngrams

    def _grams(self, text: str) -> list[str]:
        t = " " + canonicalize(text).lower() + " "
        out: list[str] = []
        for n in self.ngrams:
            out.extend(t[i : i + n] for i in range(max(0, len(t) - n + 1)))
        # whole words carry extra weight
        out.extend(w for w in t.split() for _ in range(2))
        return out

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        m = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for g in self._grams(text):
                h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "little")
                m[row, h % self.dim] += 1.0 if (h >> 63) == 0 else -1.0
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return m / norms


class SentenceTransformerEmbedder:
    """``deepvk/USER-bge-m3`` through sentence-transformers (CPU by default)."""

    name = "deepvk/USER-bge-m3"

    def __init__(self, model_name: str = "deepvk/USER-bge-m3", device: str = "cpu") -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

        self.name = model_name
        self._model = SentenceTransformer(model_name, device=device)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        vecs = self._model.encode(list(texts), normalize_embeddings=True, convert_to_numpy=True, batch_size=16)
        return np.asarray(vecs, dtype=np.float32)


def default_embedder(prefer_model: bool = True) -> Embedder:
    if prefer_model:
        try:
            return SentenceTransformerEmbedder()
        except Exception as exc:  # pragma: no cover - model not installed in CI
            logging.getLogger(__name__).info("bge-m3 unavailable (%s); using hash embedder", exc)
    return HashEmbedder()


# Seed examples per intent (paraphrases). Extend from clustered call transcripts (tools/cluster_calls.py).
EXAMPLES: dict[str, list[str]] = {
    "book": [
        "хочу записаться к врачу", "запишите меня на приём к терапевту", "мне нужно попасть к хирургу",
        "можно записаться к кардиологу на этой неделе", "нужен талон к окулисту", "хотел бы записать ребёнка к педиатру",
        "запись к стоматологу", "мне бы к неврологу на завтра",
    ],
    "reschedule": [
        "хочу перенести запись", "можно поменять время приёма", "перенесите меня на другой день",
        "нужно изменить дату записи", "давайте перезапишемся на следующую неделю", "сдвиньте приём на вечер",
    ],
    "cancel": [
        "отмените мою запись", "я не смогу прийти на приём", "хочу отказаться от записи", "снимите запись к врачу",
        "аннулируйте талон", "не приду завтра, уберите запись",
    ],
    "confirm": [
        "подтверждаю запись", "да, приду", "всё в силе, буду", "подтверждаю приём на завтра", "да, всё верно",
    ],
    "my_booking": [
        "когда у меня запись", "во сколько у меня приём", "напомните дату моей записи", "к кому я записан",
        "проверьте, есть ли у меня запись", "на какое число я записана",
    ],
    "info_schedule": [
        "какой график работы поликлиники", "до скольки вы работаете", "во сколько открывается регистратура",
        "когда принимает терапевт Иванова", "работаете ли вы в субботу", "часы работы лаборатории",
    ],
    "info_holidays": [
        "как вы работаете в праздники", "работает ли поликлиника четвёртого ноября", "график на новогодние праздники",
        "выходные дни в мае", "принимаете ли в праздничные дни",
    ],
    "info_address": [
        "какой у вас адрес", "как до вас добраться", "где находится поликлиника", "подскажите адрес филиала",
    ],
    "info_docs": [
        "какие документы нужны на приём", "нужен ли полис", "что взять с собой к врачу", "как прикрепиться к поликлинике",
    ],
    "referral": [
        "нужно ли направление к кардиологу", "у меня есть направление от терапевта", "без направления примут",
    ],
    "operator": [
        "соедините с оператором", "переключите на регистратуру", "хочу поговорить с человеком", "позовите сотрудника",
        "мне нужен живой человек, а не робот",
    ],
    "complaint": [
        "хочу подать жалобу", "мне нахамили в регистратуре", "соедините с главным врачом, у меня претензия",
        "безобразие, врач не принял", "хочу пожаловаться на отношение персонала",
    ],
    "emergency": [
        "у меня острая боль", "очень сильно болит, срочно нужен врач", "задыхаюсь, что делать", "плохо с сердцем",
        "у ребёнка температура сорок", "идёт кровь, не останавливается",
    ],
    "medical": [
        "можно ли пить эти таблетки вместе", "какая дозировка лекарства", "у меня болит голова третий день, что принять",
        "готовы ли результаты анализов", "нужен рецепт на лекарство", "продлите больничный",
    ],
}


@dataclass
class EmbedPrediction:
    intent: str
    similarity: float
    margin: float
    runner_up: Optional[str] = None
    accepted: bool = False


@dataclass
class EmbeddingIntentClassifier:
    embedder: Embedder
    examples: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in EXAMPLES.items()})
    threshold: float = 0.35
    margin: float = 0.03
    _labels: list[str] = field(default_factory=list, init=False, repr=False)
    _matrix: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    _centroids: dict[str, np.ndarray] = field(default_factory=dict, init=False, repr=False)

    def fit(self) -> "EmbeddingIntentClassifier":
        texts: list[str] = []
        labels: list[str] = []
        for intent, exs in self.examples.items():
            for e in exs:
                texts.append(e)
                labels.append(intent)
        self._matrix = self.embedder.encode(texts)
        self._labels = labels
        for intent in self.examples:
            rows = self._matrix[[i for i, l in enumerate(labels) if l == intent]]
            c = rows.mean(axis=0)
            n = np.linalg.norm(c)
            self._centroids[intent] = c / n if n else c
        return self

    def predict(self, text: str) -> EmbedPrediction:
        if self._matrix is None:
            self.fit()
        assert self._matrix is not None
        v = self.embedder.encode([text])[0]
        sims = self._matrix @ v
        # per-intent score = 0.5 * best example + 0.5 * centroid similarity
        scores: dict[str, float] = {}
        for intent, c in self._centroids.items():
            idx = [i for i, l in enumerate(self._labels) if l == intent]
            best_ex = float(sims[idx].max()) if idx else -1.0
            scores[intent] = 0.5 * best_ex + 0.5 * float(c @ v)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top, top_s = ranked[0]
        second, second_s = ranked[1] if len(ranked) > 1 else (None, -1.0)
        margin = top_s - second_s
        accepted = top_s >= self.threshold and margin >= self.margin
        return EmbedPrediction(top if accepted else "unknown", round(top_s, 4), round(margin, 4), second, accepted)

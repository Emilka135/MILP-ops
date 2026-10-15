"""Simulated ASR: deterministic corruption of a spoken sentence the way a telephony recogniser would.

Models the error classes seen in GigaAM / T-one / Vosk telephony output:
* no punctuation / casing, ё→е;
* inverse text normalisation applied inconsistently (number words ↔ digits);
* dropped short function words and inserted fillers ("э", "ну", "это", "значит");
* occasional in-word character errors away from the word stem;
* truncated final word (Deeray runtime issue #1).
"""

from __future__ import annotations

import random
import re

from calls.tts.verbalize import number_to_words

_FILLERS = ["э", "ну", "это", "значит", "вот", "как бы"]
_DROPPABLE = {"пожалуйста", "мне", "бы", "вот", "это", "ну", "а", "и", "же"}
_NUM_WORD = re.compile(r"\b(\d+)\b")


def corrupt(text: str, seed: int, severity: float = 0.5) -> str:
    rnd = random.Random(seed)
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[.,!?;:«»\"()\-]", " ", t)
    words = t.split()
    out: list[str] = []
    for w in words:
        r = rnd.random()
        if w in _DROPPABLE and r < 0.3 * severity:
            continue  # dropped function word
        if w.isdigit() and rnd.random() < 0.5:
            w = number_to_words(int(w))  # ITN not applied
        elif rnd.random() < 0.15 * severity and len(w) >= 7:
            i = rnd.randrange(4, len(w) - 1)  # keep the stem intact, break the ending
            w = w[:i] + w[i + 1] + w[i] + w[i + 2 :]
        if rnd.random() < 0.08 * severity:
            out.append(rnd.choice(_FILLERS))
        out.append(w)
    if out and rnd.random() < 0.1 * severity and len(out[-1]) > 5:
        out[-1] = out[-1][:-1]  # truncated final word
    return " ".join(out)

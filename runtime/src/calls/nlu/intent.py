"""Rule-based intent classifier and entity extractor for registry calls.

Deterministic, explainable and fast (tens of microseconds): regex stems over the
normalised utterance.  It is the first stage of the "мясорубка" – everything the
rules can classify with confidence goes straight to the scenario switch-case;
the rest is handed to the embedding mini-agent (``embed.py``) and, if that is
unsure too, to a human operator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from .normalize import Interpretation, interpret

# Order = priority when several intents match with the same score.
INTENTS: tuple[str, ...] = (
    "emergency",
    "complaint",
    "operator",
    "medical",
    "reschedule",
    "cancel",
    "confirm",
    "my_booking",
    "book",
    "referral",
    "info_holidays",
    "info_schedule",
    "info_address",
    "info_docs",
)

# Which intents the switch-case can fulfil without a human.
SELF_SERVICE: frozenset[str] = frozenset(
    {"book", "reschedule", "cancel", "confirm", "my_booking", "info_holidays", "info_schedule", "info_address", "info_docs", "referral"}
)
# Which intents must *always* go to a human (case requirement).
HUMAN_ONLY: frozenset[str] = frozenset({"emergency", "complaint", "medical", "operator"})

_P: dict[str, list[tuple[str, float]]] = {
    "emergency": [
        (r"\bостр\w* бол", 3.0), (r"\bочень\s+(сильно\s+)?бол", 2.5), (r"\bневыносим", 2.5), (r"\bскор(ая|ую)\b", 2.0),
        (r"\bзадыха", 3.0), (r"\bкров(ь|отеч)", 2.0), (r"\bтеряю сознание|\bпотерял\w* сознание", 3.0), (r"\bсрочно\b", 1.0),
        (r"\bтемпература (39|40|сорок|тридцать девять)", 2.5), (r"\bсильн\w+ бол", 2.0), (r"\bплохо с сердцем", 3.0),
        (r"\bумира", 3.0), (r"\bприступ", 2.5), (r"\bнечем дышать", 3.0),
    ],
    "complaint": [
        (r"\bжалоб", 3.0), (r"\bжаловаться|\bпожаловаться", 3.0), (r"\bпретензи", 3.0), (r"\bхам(ств|ил|ит)", 2.5),
        (r"\bбезобрази", 2.5), (r"\bглавн\w+ врач", 2.0), (r"\bглаввр", 2.0), (r"\bвозмущ", 2.0), (r"\bнагруби", 2.5),
        (r"\bотношени[ея] персонал", 2.0), (r"\bнедовол", 2.0),
    ],
    "operator": [
        (r"\bоператор", 3.0), (r"\bжив(ой|ого|ым) (человек|сотрудник)", 3.0), (r"\bчеловек(а|ом|у)?\b", 1.0),
        (r"\bрегистратор", 2.5), (r"\bсоедини", 2.5), (r"\bпереключи", 2.5), (r"\bпозови", 2.0), (r"\bдевушк", 1.5),
        (r"\bсотрудник", 1.5), (r"\bне робот", 2.0),
    ],
    "medical": [
        (r"\bлекарств", 2.5), (r"\bтаблетк", 2.5), (r"\bдозировк", 3.0), (r"\bрецепт", 2.5), (r"\bсимптом", 2.5),
        (r"\bдиагноз", 2.5), (r"\bанализ", 1.5), (r"\bбольничн", 2.0), (r"\bсправк", 1.5), (r"\bчто (мне )?(принимать|пить|делать)", 2.0),
        (r"\bможно ли (пить|принимать)", 2.5), (r"\bболит\b", 1.5), (r"\bтемператур", 1.0), (r"\bкашел|\bкашля", 1.5),
        (r"\bрезультат", 1.5), (r"\bдавлени", 1.5), (r"\bаллерги", 1.0), (r"\bпобочн", 2.0), (r"\bлечени", 1.5),
    ],
    "reschedule": [
        (r"\bперенес", 3.0), (r"\bперенос", 3.0), (r"\bперезаписат", 3.0), (r"\bпоменя\w* (время|дату|день|запись)", 2.5),
        (r"\bизменит\w* (время|дату|день|запись)", 2.5), (r"\bдруго[ейм] (время|день|дату|число)", 2.0), (r"\bна друг", 1.5),
        (r"\bсдвину", 2.0), (r"\bпередвину", 2.0),
    ],
    "cancel": [
        (r"\bотмен", 3.0), (r"\bне (с)?(мог|могу|смогу)\w* (прийти|придти|приехать|быть|подойти)", 2.5), (r"\bне приду\b", 2.5),
        (r"\bотказ", 2.0), (r"\bснят\w* запись|\bснимите запись|\bубрать запись|\bуберите запись", 2.5), (r"\bаннулир", 2.5),
        (r"\bне нужн\w* (запись|прием)", 2.0), (r"\bне буду приходить", 2.5),
    ],
    "confirm": [
        (r"\bподтвержда", 3.0), (r"\bподтвердит", 2.5), (r"\bприду\b", 2.0), (r"\bбуду\b", 1.0), (r"\bв силе", 2.5),
        (r"\bвсе верно|\bвсе правильно", 2.0), (r"\bобязательно (буду|приду)", 2.0),
    ],
    "my_booking": [
        (r"\bкогда (у меня )?(запись|прием)", 3.0), (r"\bво сколько (у меня )?(запись|прием)", 3.0), (r"\bмо[яюей] запис", 2.5),
        (r"\bзаписан\w*( ли)? (я|он|она)", 2.5), (r"\bуточнит\w* (запись|время|дату)", 2.0), (r"\bнапомни\w* (запись|время|дату|когда)", 2.0),
        (r"\bпровер\w* запис", 2.5), (r"\bесть ли (у меня )?запис", 2.5), (r"\bк кому я записан", 3.0), (r"\bна какое (число|время) (я )?записан", 3.0),
    ],
    "book": [
        (r"\bзаписат", 3.0), (r"\bзапишит", 3.0), (r"\bзапись\b", 1.5), (r"\bзаписи\b", 1.0), (r"\bзаписаться", 3.0),
        (r"\bпопасть к", 2.5), (r"\bна прием\b", 1.5), (r"\bнужен врач|\bнужна врач|\bнужен доктор", 2.0), (r"\bхочу к\b", 2.0),
        (r"\bталон", 2.5), (r"\bпринима\w+ ли", 1.0), (r"\bможно (к|на)\b", 1.0), (r"\bмне (бы )?к\b", 2.0), (r"\bхотел\w* бы к", 2.0),
    ],
    "referral": [
        (r"\bнаправлени", 3.0), (r"\bнаправил", 2.0),
    ],
    "info_holidays": [
        (r"\bпраздни", 3.0), (r"\bвыходн", 2.0), (r"\bновогодн", 3.0), (r"\bв празд", 3.0), (r"\bнерабоч", 2.5),
        (r"\b(4|четвертого) ноября", 2.0), (r"\bмайск", 2.0), (r"\bработает(е)? ли .*(субботу|воскресенье|праздник)", 2.0),
    ],
    "info_schedule": [
        (r"\bрасписани", 3.0), (r"\bграфик", 3.0), (r"\bчасы работы", 3.0), (r"\bрежим работы", 3.0), (r"\bдо скольки", 2.5),
        (r"\bсо скольки", 2.5), (r"\bво сколько (открыва|закрыва|работа)", 2.5), (r"\bкогда (принимает|работает)", 2.5),
        (r"\bработает(е)? ли", 1.5), (r"\bпринимает ли", 1.5), (r"\bкакие дни", 2.0),
    ],
    "info_address": [
        (r"\bадрес", 3.0), (r"\bкак (к вам )?(добраться|доехать|пройти|проехать)", 3.0), (r"\bгде (вы )?(находит|располож)", 3.0),
        (r"\bкакой у вас телефон|\bномер телефона регистратуры", 2.5), (r"\bкакой корпус|\bкакой этаж|\bкакой кабинет", 2.0),
    ],
    "info_docs": [
        (r"\bкакие документы", 3.0), (r"\bнужен ли (полис|паспорт|снилс)", 3.0), (r"\bполис", 1.5), (r"\bпаспорт", 1.0),
        (r"\bчто (с собой )?(взять|брать|принести)", 2.5), (r"\bприкрепит", 2.0), (r"\bприкреплени", 2.0),
    ],
}
_COMPILED: dict[str, list[tuple[re.Pattern[str], float]]] = {k: [(re.compile(p), w) for p, w in v] for k, v in _P.items()}

# --- entities -------------------------------------------------------------------------

SPECIALTIES: dict[str, tuple[str, ...]] = {
    "терапевт": ("терапевт",), "педиатр": ("педиатр", "детск"), "хирург": ("хирург",),
    "кардиолог": ("кардиолог", "сердц", "сердеч"), "невролог": ("невролог", "невропатолог"),
    "офтальмолог": ("офтальмолог", "окулист", "глазн"), "отоларинголог": ("отоларинголог", "лор", "ухо горло"),
    "стоматолог": ("стоматолог", "зубн", "зуб"), "гинеколог": ("гинеколог", "женск"), "уролог": ("уролог",),
    "дерматолог": ("дерматолог", "кожн", "дерматовенеролог"), "эндокринолог": ("эндокринолог", "щитовидк"),
    "гастроэнтеролог": ("гастроэнтеролог", "гастро", "желуд"), "травматолог": ("травматолог", "травмпункт", "травма"),
    "ортопед": ("ортопед",), "онколог": ("онколог",), "психиатр": ("психиатр",), "психотерапевт": ("психотерапевт", "психолог"),
    "ревматолог": ("ревматолог",), "пульмонолог": ("пульмонолог",), "аллерголог": ("аллерголог", "иммунолог"),
    "инфекционист": ("инфекционист",), "физиотерапевт": ("физиотерап", "физио"), "узи": ("узи", "ультразвук"),
    "флюорография": ("флюорограф", "флюшк", "рентген"), "лаборатория": ("лаборатор", "сдать кровь", "забор крови"),
    "прививка": ("прививк", "вакцин"), "массаж": ("массаж",), "невролог детский": (),
}
# Specialists that require a therapist referral in this clinic (configurable per deployment).
REFERRAL_REQUIRED: frozenset[str] = frozenset(
    {"кардиолог", "невролог", "эндокринолог", "гастроэнтеролог", "ревматолог", "пульмонолог", "аллерголог", "онколог", "психиатр", "физиотерапевт", "узи"}
)
_OTHER_PERSON = re.compile(
    r"\b(ребен[ок|ка|ку]\w*|сын\w*|доч\w*|мам[ау]|мать|отц[ау]|папу|пап[ае]|муж[ау]?|жен[ау]|бабушк\w*|дедушк\w*|родственни\w*|внук\w*|внучк\w*|супруг\w*|брат\w*|сестр\w*|для другого|другого человека|не себя|не для себя)\b"
)
_SELF = re.compile(r"\b(себя|меня|мне|сам[аи]?|я)\b")
_SURNAME_SUFFIX = re.compile(
    r"^[а-я]{2,}(ов|ова|ову|овой|овым|ев|ева|еву|евой|евым|ин|ина|ину|иной|иным|ын|ына|ыну|ский|ская|скому|ской|ским"
    r"|цкий|цкая|цкому|цкой|енко|ич|ичу|ук|юк|их|ых|ко|ян|яну|дзе|швили)$"
)
_SURNAME_CUES = ("доктор", "доктору", "врач", "врачу", "к", "у", "доктора", "врача")


@dataclass
class Entities:
    specialty: Optional[str] = None
    needs_referral: bool = False
    doctor_surname: Optional[str] = None
    for_other: Optional[bool] = None  # True = booking for somebody else, False = for self, None = unknown
    interpretation: Optional[Interpretation] = None


@dataclass
class IntentResult:
    intent: str  # one of INTENTS or "unknown"
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)
    entities: Entities = field(default_factory=Entities)
    matched: list[str] = field(default_factory=list)

    @property
    def self_service(self) -> bool:
        return self.intent in SELF_SERVICE

    @property
    def human_only(self) -> bool:
        return self.intent in HUMAN_ONLY


def score_intents(text: str) -> dict[str, float]:
    t = " " + text.lower().replace("ё", "е") + " "
    scores: dict[str, float] = {}
    for intent, pats in _COMPILED.items():
        s = 0.0
        for pat, w in pats:
            if pat.search(t):
                s += w
        if s:
            scores[intent] = s
    return scores


def extract_specialty(text: str) -> Optional[str]:
    t = text.lower().replace("ё", "е")
    best: Optional[tuple[int, str]] = None
    for name, stems in SPECIALTIES.items():
        for stem in (name,) + stems:
            if not stem:
                continue
            pos = t.find(stem)
            if pos >= 0 and (best is None or pos < best[0] or (pos == best[0] and len(stem) > len(best[1]))):
                # "лор" must be a whole word to avoid matching "флюорография"
                if stem == "лор" and not re.search(r"\bлор(а|у|ом|е)?\b", t):
                    continue
                if stem == "зуб" and "зубн" in t:
                    continue
                best = (pos, name)
    return best[1] if best else None


def extract_surname(text: str, known: Iterable[str] = ()) -> Optional[str]:
    toks = re.findall(r"[а-яё]+", text.lower().replace("ё", "е"))
    known_l = {k.lower().replace("ё", "е") for k in known}
    for tok in toks:
        if tok in known_l:
            return tok.capitalize()
    for i, tok in enumerate(toks):
        prev = toks[i - 1] if i else ""
        if prev in _SURNAME_CUES and _SURNAME_SUFFIX.match(tok) and tok not in _SPECIALTY_WORDS:
            return tok.capitalize()
    return None


_SPECIALTY_WORDS = {w for stems in SPECIALTIES.values() for w in stems} | set(SPECIALTIES)


def extract_entities(text: str, today: Optional[date] = None, known_surnames: Iterable[str] = ()) -> Entities:
    spec = extract_specialty(text)
    t = text.lower().replace("ё", "е")
    for_other: Optional[bool] = None
    if _OTHER_PERSON.search(t):
        for_other = True
    elif _SELF.search(t):
        for_other = False
    return Entities(
        specialty=spec,
        needs_referral=spec in REFERRAL_REQUIRED if spec else False,
        doctor_surname=extract_surname(text, known_surnames),
        for_other=for_other,
        interpretation=interpret(text, today),
    )


def classify(text: str, today: Optional[date] = None, known_surnames: Iterable[str] = (), threshold: float = 1.5) -> IntentResult:
    """Classify an utterance; ``unknown`` when no rule fires above ``threshold``."""
    scores = score_intents(text)
    ents = extract_entities(text, today, known_surnames)
    if ents.specialty or ents.doctor_surname:
        if not scores:
            scores["book"] = 1.5  # "мне к кардиологу" / "к доктору Ивановой" – implies a booking request
        elif max(scores, key=scores.__getitem__) == "book":
            scores["book"] = max(scores["book"], 1.5)  # "можно к окулисту в 9:15" – weak cue + specialty
    if not scores:
        return IntentResult("unknown", 0.0, scores, ents)
    best = max(scores.values())
    ranked = [i for i in INTENTS if scores.get(i, 0.0) == best]
    intent = ranked[0]
    if best < threshold:
        return IntentResult("unknown", round(best / (threshold * 2), 2), scores, ents)
    second = max((v for k, v in scores.items() if k != intent), default=0.0)
    confidence = min(0.97, 0.55 + 0.1 * best - 0.05 * second)
    return IntentResult(intent, round(max(confidence, 0.5), 2), scores, ents)

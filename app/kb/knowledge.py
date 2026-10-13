"""База знаний регистратуры из регламента РГ-ТЕЛ-01.

Контракт — ARCHITECTURE.md 3.4:

    kb = KnowledgeBase()
    entry = kb.answer("сколько стоит УЗИ щитовидки")
    entry.answer       # «УЗИ щитовидной железы стоит тысяча триста рублей. Записать вас?»
    entry.route        # «2.6, 6.7, С-09»

Источник — разделы 2.1–2.6 (справочные сведения) и 6.1–6.9 (скрипты ответов)
регламента. Ответы короткие, для произнесения вслух: не длиннее 20 слов, без
аббревиатур, с предложением следующего шага.

Правило «не выдумывать»: чего нет в справочнике — метод ``answer`` возвращает
``None``, и вызывающий переводит пациента на регистратора (R-08/R-10).
Значения анализов база знаний не содержит вообще: по телефону они не
сообщаются (врачебная тайна), а в МИС ассистент видит только статус
готовности, поэтому ``answer`` никогда не обращается к стенду за значениями.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, TYPE_CHECKING

try:  # обычный запуск из корня репозитория
    from app.types import Client, KbEntry
except ImportError:  # pragma: no cover — запуск файла напрямую
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.types import Client, KbEntry

if TYPE_CHECKING:  # pragma: no cover — app.mis принадлежит teammate-b
    from app.mis.client import MisClient


_MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_KB_PATH = "app/kb/kb.json"

#: Предел длины реплики для произнесения вслух (раздел 3.2 регламента).
MAX_ANSWER_WORDS = 20

_FALLBACK_KB: dict[str, Any] = {
    "version": "fallback",
    "source": "встроенная страховка (разделы 2.1–2.6, 6.1–6.9)",
    "contacts": {
        "адрес": "ул. Центральная, д. 7",
        "единый_номер": "+7 (000) 700-07-07",
        "скорая": "103",
        "единый_экстренный": "112",
        "колл_центр_минздрава": "122",
    },
    "working_hours": {
        "поликлиника_будни": "7:30–20:00",
        "поликлиника_суббота": "9:00–15:00",
        "поликлиника_воскресенье": "выходной",
    },
    "lab_terms": {"Общий анализ крови": "1 рабочий день"},
    "lab_terms_speech": {"Общий анализ крови": "за один рабочий день"},
    "waiting_terms": {"Приём участкового терапевта или педиатра": "не более 24 часов"},
    "prices": [],
    "entries": [
        {"key": "hours", "topic": "Режим работы", "route": "2.2, 6.1",
         "answer": "Поликлиника работает по будням с семи тридцати до двадцати, в субботу с девяти до пятнадцати. Воскресенье — выходной.",
         "keywords": ["работаете", "режим работы", "до скольки", "суббота"]},
        {"key": "emergency_numbers", "topic": "Экстренные номера", "route": "2.1",
         "answer": "При угрозе жизни звоните сто три или сто двенадцать. Это круглосуточно и бесплатно.",
         "keywords": ["скорая", "103", "112"]},
        {"key": "medical_boundary", "topic": "Медицинский вопрос", "route": "3.8, 6.9",
         "answer": "Я не могу давать медицинские советы — это может сделать только врач. Записать вас?",
         "keywords": ["что выпить", "таблетки", "лекарство", "симптомы"]},
        {"key": "operator_connect", "topic": "Соединить с человеком", "route": "6.9, R-02",
         "answer": "Соединяю с сотрудником.", "keywords": ["оператор", "соедините", "человек"]},
    ],
    "price_unknown_answer": "Эту услугу уточнит платное отделение. Соединяю.",
    "price_unknown_topic": "Платные услуги вне справочника",
    "price_unknown_route": "6.7, R-10",
}


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------

_NON_WORD_RE = re.compile(r"[^а-яa-z0-9]+")

#: Слова, однозначно означающие вопрос о цене (вопрос «почему» сюда не попадает).
_PRICE_TOKENS = frozenset({"стоимость", "цена", "цены", "цену", "ценам", "прайс", "почем"})


def _normalize(text: Any) -> str:
    if isinstance(text, str):
        raw = text
    elif text is None:
        return ""
    else:
        try:
            raw = str(text)
        except Exception:  # pragma: no cover
            return ""
    return " ".join(_NON_WORD_RE.sub(" ", raw.lower().replace("ё", "е")).split())


def _prefix_regex(word: str) -> str:
    word = word.replace("ё", "е")
    if len(word) <= 3:
        return re.escape(word)
    return re.escape(word[: max(4, len(word) - 2)]) + r"\w*"


def _phrase_regex(phrase: str, gap: int = 2) -> Optional[re.Pattern[str]]:
    parts = [part for part in _normalize(phrase).split() if part]
    if not parts:
        return None
    if len(parts) == 1:
        body = _prefix_regex(parts[0])
    else:
        joiner = r"\s+(?:\w+\s+){0,%d}" % gap
        body = joiner.join(_prefix_regex(part) for part in parts)
    try:
        return re.compile(r"(?<!\w)" + body + r"(?!\w)")
    except re.error:  # pragma: no cover
        return None


def _word_hit(words: Iterable[str], keyword: str) -> bool:
    keyword = keyword.replace("ё", "е")
    if len(keyword) <= 3:
        return any(word == keyword for word in words)
    stem = keyword[: max(4, len(keyword) - 2)]
    return any(word.startswith(stem) for word in words)


_UNITS_MALE = ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_UNITS_FEMALE = ("ноль", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_TEENS = ("десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать")
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот")


def _plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _triple(number: int, *, feminine: bool = False) -> str:
    parts: list[str] = []
    hundreds, rest = divmod(number, 100)
    if hundreds:
        parts.append(_HUNDREDS[hundreds])
    tens, units = divmod(rest, 10)
    if tens == 1:
        parts.append(_TEENS[units])
    else:
        if tens:
            parts.append(_TENS[tens])
        if units:
            parts.append((_UNITS_FEMALE if feminine else _UNITS_MALE)[units])
    return " ".join(part for part in parts if part)


def num_to_words(number: int) -> str:
    """Число словами для произнесения вслух: 1300 -> «тысяча триста»."""
    try:
        number = int(number)
    except Exception:  # pragma: no cover
        return ""
    if number < 0:
        return "минус " + num_to_words(-number)
    if number == 0:
        return "ноль"
    if number >= 1_000_000_000:
        return str(number)
    parts: list[str] = []
    millions, rest = divmod(number, 1_000_000)
    thousands, remainder = divmod(rest, 1000)
    if millions:
        parts.append(_triple(millions))
        parts.append(_plural(millions, "миллион", "миллиона", "миллионов"))
    if thousands:
        if thousands == 1:
            parts.append("тысяча")
        else:
            parts.append(_triple(thousands, feminine=True))
            parts.append(_plural(thousands, "тысяча", "тысячи", "тысяч"))
    if remainder or not parts:
        parts.append(_triple(remainder))
    return " ".join(part for part in parts if part)


def _load_json(path_str: str) -> tuple[dict[str, Any], str]:
    candidates: list[Path] = []
    if path_str:
        candidates.append(Path(path_str))
        candidates.append(_MODULE_DIR / Path(path_str).name)
    candidates.append(_MODULE_DIR / "kb.json")
    for candidate in candidates:
        try:
            if candidate.is_file():
                with candidate.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    return data, str(candidate)
        except Exception:
            continue
    return _FALLBACK_KB, "встроенный справочник"


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_str_dict(value: Any) -> dict[str, str]:
    return {str(key): str(item) for key, item in _as_dict(value).items()}


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()]


class _Entry:
    """Запись справочника вместе со служебными полями поиска."""

    __slots__ = ("entry", "keywords", "phrases", "priority", "index", "weight")

    def __init__(self, entry: KbEntry, keywords: list[Any], priority: int, index: int) -> None:
        self.entry = entry
        self.priority = priority
        self.index = index
        self.weight = 0
        self.keywords: list[str] = []
        self.phrases: list[tuple[re.Pattern[str], str]] = []
        for keyword in keywords:
            text = _normalize(keyword)
            if not text:
                continue
            if " " in text:
                regex = _phrase_regex(text)
                if regex is not None:
                    self.phrases.append((regex, text))
            else:
                self.keywords.append(text)


class KnowledgeBase:
    """Справочник ответов регистратуры. Методы не выбрасывают исключений."""

    def __init__(self, *, path: str = DEFAULT_KB_PATH) -> None:
        self.path = path or DEFAULT_KB_PATH
        self._data, self._loaded_from = _load_json(self.path)
        self._build()

    # -- сборка ------------------------------------------------------------

    def _build(self) -> None:
        data = self._data if isinstance(self._data, dict) else {}
        self._contacts = _as_str_dict(data.get("contacts"))
        self._working_hours = _as_str_dict(data.get("working_hours"))
        self._lab_terms = _as_str_dict(data.get("lab_terms"))
        self._lab_speech = _as_str_dict(data.get("lab_terms_speech")) or dict(self._lab_terms)
        self._waiting_terms = _as_str_dict(data.get("waiting_terms"))

        self._price_items: list[dict[str, Any]] = []
        self._prices: dict[str, int] = {}
        for raw in data.get("prices") if isinstance(data.get("prices"), list) else []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            try:
                price = int(raw.get("price") or 0)
            except Exception:
                continue
            item = dict(raw)
            item["name"] = name
            item["price"] = price
            item["speech"] = str(raw.get("speech") or name)
            item["aliases"] = [str(alias) for alias in raw.get("aliases") or [] if str(alias).strip()]
            item["weak_aliases"] = [str(alias) for alias in raw.get("weak_aliases") or [] if str(alias).strip()]
            item["_alias_res"] = [(alias, _phrase_regex(alias)) for alias in item["aliases"]]
            item["_weak_res"] = [(alias, _phrase_regex(alias)) for alias in item["weak_aliases"]]
            self._price_items.append(item)
            self._prices[name] = price

        self._entries: list[_Entry] = []
        self._by_key: dict[str, KbEntry] = {}
        index = 0
        raw_entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            answer = str(raw.get("answer") or "").strip()
            if not answer:
                continue
            entry = KbEntry(
                topic=str(raw.get("topic") or ""),
                route=str(raw.get("route") or ""),
                answer=answer,
                next_action=str(raw.get("next_action") or ""),
                follow_up=str(raw.get("follow_up") or ""),
            )
            try:
                priority = int(raw.get("priority") or 1)
            except Exception:
                priority = 1
            keywords = raw.get("keywords") if isinstance(raw.get("keywords"), list) else []
            self._entries.append(_Entry(entry, keywords, priority, index))
            key = str(raw.get("key") or "").strip()
            if key and key not in self._by_key:
                self._by_key[key] = entry
            index += 1

        self._price_unknown_answer = str(data.get("price_unknown_answer")
                                         or "Эту услугу уточнит платное отделение. Соединяю.")
        self._price_unknown_topic = str(data.get("price_unknown_topic") or "Платные услуги")
        self._price_unknown_route = str(data.get("price_unknown_route") or "6.7, R-10")

        self._price_intent_res = [
            _phrase_regex(phrase) for phrase in (
                "сколько стоит", "сколько стоят", "сколько будет стоить", "сколько денег",
            )
        ]
        self._lab_aliases: list[tuple[str, re.Pattern[str]]] = []
        raw_aliases = data.get("lab_aliases") if isinstance(data.get("lab_aliases"), dict) else {}
        for name, aliases in raw_aliases.items():
            name = str(name)
            if isinstance(aliases, str):
                aliases = [aliases]
            if not isinstance(aliases, list):
                continue
            for alias in aliases:
                regex = _phrase_regex(str(alias))
                if regex is not None:
                    self._lab_aliases.append((name, regex))
        self._lab_aliases.sort(key=lambda pair: len(pair[0]), reverse=True)

        # Услуги, которых в справочнике поликлиники нет: честный None (R-08/R-10),
        # а не выдуманный ответ и не «режим работы» в ответ на другой вопрос.
        self._unknown_services: list[str] = []
        for service in _as_str_list(data.get("unknown_services")):
            normalized = _normalize(service)
            if normalized:
                self._unknown_services.append(normalized)

    # -- публичный контракт ------------------------------------------------

    def answer(self, question: str, *, patient: Optional[Client] = None,
               mis: Optional["MisClient"] = None) -> Optional[KbEntry]:
        """Подобрать ответ на вопрос пациента. Нет ответа — ``None``.

        ``patient`` и ``mis`` принимаются по контракту, но значения анализов из
        стенда не читаются: ассистент видит только статус готовности (С-08),
        значения сообщает врач.
        """
        try:
            text = _normalize(question)
            if not text:
                return None
            words = text.split()

            if self._is_price_question(text):
                item = self._best_price_item(text)
                return self._price_entry(item) if item else self._price_unknown_entry()

            if self._mentions_unknown_service(text, words):
                return None

            lab = self._lab_term_entry(text)
            if lab is not None:
                return lab

            best: Optional[_Entry] = None
            best_score = 0
            best_weight = 0
            for candidate in self._entries:
                score, weight = self._score(candidate, text, words)
                if score <= 0:
                    continue
                better = (
                    score > best_score
                    or (score == best_score and weight > best_weight)
                    or (score == best_score and weight == best_weight
                        and best is not None and candidate.priority > best.priority)
                )
                if better:
                    best = candidate
                    best_score = score
                    best_weight = weight
            if best is None or best_score < 2:
                return None
            return best.entry
        except Exception:  # pragma: no cover — справочник не должен ломать диалог
            return None

    def topic(self, key: str) -> Optional[KbEntry]:
        """Запись справочника по ключу (``hours``, ``cancel``, ``price``...)."""
        try:
            return self._by_key.get(str(key or "").strip()) or None
        except Exception:  # pragma: no cover
            return None

    def prices(self) -> dict[str, int]:
        return dict(self._prices)

    def working_hours(self) -> dict[str, str]:
        return dict(self._working_hours)

    def lab_terms(self) -> dict[str, str]:
        return dict(self._lab_terms)

    # -- дополнительные сведения ------------------------------------------

    def waiting_terms(self) -> dict[str, str]:
        """Максимальные сроки ожидания по ПГГ (раздел 2.4)."""
        return dict(self._waiting_terms)

    def contacts(self) -> dict[str, str]:
        return dict(self._contacts)

    def all_entries(self) -> list[KbEntry]:
        return [item.entry for item in self._entries]

    def keys(self) -> list[str]:
        return list(self._by_key)

    def price_entry(self, service: str) -> Optional[KbEntry]:
        """Цена одной услуги из справочника (без выдумывания)."""
        try:
            text = _normalize(service)
            item = self._best_price_item(text)
            return self._price_entry(item) if item else None
        except Exception:  # pragma: no cover
            return None

    def specialty_hint(self, text: str) -> Optional[str]:
        """Подсказка «с какого врача начать» для неспецифичных жалоб (раздел 3.8)."""
        try:
            normalized = _normalize(text)
            if any(word in normalized for word in ("ребенок", "ребенку", "сын", "дочь", "дочка", "малыш")):
                return "педиатр"
            if any(word in normalized for word in ("беременность", "гинеколог", "женский")):
                return "акушер-гинеколог"
            if any(word in normalized for word in ("зуб", "зубная", "десна")):
                return "стоматолог"
            return "терапевт"
        except Exception:  # pragma: no cover
            return None

    def too_long_answers(self) -> list[tuple[str, int]]:
        """Реплики длиннее 20 слов — для самопроверки и панели демо."""
        result: list[tuple[str, int]] = []
        for item in self._entries:
            count = len(item.entry.answer.split())
            if count > MAX_ANSWER_WORDS:
                result.append((item.entry.topic, count))
        return result

    def stats(self) -> dict[str, Any]:
        return {
            "source": self._loaded_from,
            "entries": len(self._entries),
            "prices": len(self._prices),
            "lab_terms": len(self._lab_terms),
            "working_hours_rows": len(self._working_hours),
            "too_long": len(self.too_long_answers()),
        }

    # -- внутренняя логика -------------------------------------------------

    def _mentions_unknown_service(self, text: str, words: list[str]) -> bool:
        """Спрашивают об услуге, которой нет в справочнике (МРТ, ФГДС, массаж...)."""
        for service in self._unknown_services:
            if " " in service:
                regex = _phrase_regex(service)
                if regex is not None and regex.search(text):
                    return True
            elif _word_hit(words, service):
                return True
        return False

    def _score(self, candidate: _Entry, text: str, words: list[str]) -> tuple[int, int]:
        """(вес совпадений, «специфичность» — суммарная длина совпавших ключей)."""
        score = 0
        weight = 0
        for keyword in candidate.keywords:
            if _word_hit(words, keyword):
                score += 2
                weight += len(keyword)
        for regex, keyword in candidate.phrases:
            if regex.search(text):
                score += 3
                weight += len(keyword)
        return score, weight

    def _is_price_question(self, text: str) -> bool:
        words = text.split()
        if any(word in _PRICE_TOKENS for word in words):
            return True
        if any(word in ("стоит", "стоят", "стоить", "стоило") for word in words):
            return "не" not in words
        if "сколько" in words and any(word.startswith("сто") for word in words):
            return True
        return any(regex is not None and regex.search(text) for regex in self._price_intent_res)

    def _best_price_item(self, text: str) -> Optional[dict[str, Any]]:
        best: Optional[dict[str, Any]] = None
        best_score = 0
        for item in self._price_items:
            score = 0
            for variant in ("_alias_res", "_weak_res"):
                weight = 3 if variant == "_alias_res" else 1
                for _alias, regex in item[variant]:
                    if regex is not None and regex.search(text):
                        score = max(score, weight + min(len(_alias.split()), 2))
            if score > best_score:
                best = item
                best_score = score
        return best if best_score >= 2 else None

    def _price_entry(self, item: dict[str, Any]) -> KbEntry:
        price = int(item.get("price") or 0)
        speech = str(item.get("speech") or item.get("name") or "")
        if item.get("from"):
            price_text = str(item.get("price_speech") or ("от " + num_to_words(price)))
            answer = f"{speech} — {price_text} рублей. Записать вас?"
        else:
            answer = f"{speech} стоит {num_to_words(price)} рублей. Записать вас?"
        return KbEntry(
            topic="Платные услуги",
            route="2.6, 6.7, С-09",
            answer=answer,
            next_action="сценарий С-09",
            follow_up="называть только цены из справочника",
        )

    def _price_unknown_entry(self) -> KbEntry:
        return KbEntry(
            topic=self._price_unknown_topic,
            route=self._price_unknown_route,
            answer=self._price_unknown_answer,
            next_action="перевод в платное отделение, добавочный 3",
            follow_up="услуги нет в справочнике",
        )

    def _lab_term_entry(self, text: str) -> Optional[KbEntry]:
        intent = False
        for phrase in ("сколько делается", "сколько делается анализ", "срок готовности анализа",
                       "как долго делается", "когда будет готов анализ", "сколько ждать анализ",
                       "сколько готовится", "срок готовности"):
            regex = _phrase_regex(phrase)
            if regex is not None and regex.search(text):
                intent = True
                break
        if not intent:
            return None
        for name, regex in self._lab_aliases:
            if regex.search(text):
                return self._lab_term_answer(name)
        words = text.split()
        for name, speech in self._lab_speech.items():
            name_words = _normalize(name).split()
            hits = sum(1 for word in name_words if _word_hit(words, word))
            if name_words and hits >= max(1, len(name_words) - 1):
                return self._lab_term_answer(name)
        return None

    def _lab_term_answer(self, name: str) -> KbEntry:
        speech = self._lab_speech.get(name) or self._lab_terms.get(name, "")
        if speech and not speech.startswith("за "):
            speech = "за " + speech
        return KbEntry(
            topic="Сроки готовности анализов",
            route="2.5",
            answer=f"{name} готовят {speech}.".strip(),
            next_action="сценарий С-08",
            follow_up="значения результатов не сообщаются",
        )


__all__ = ["KnowledgeBase", "DEFAULT_KB_PATH", "MAX_ANSWER_WORDS", "num_to_words"]

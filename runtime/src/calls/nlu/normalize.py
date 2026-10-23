"""Deterministic normalisation of spoken Russian answers.

The goal is to collapse the "5 million ways to say 14" into one canonical
form *before* any classifier sees the text:

* number words → integers ("двадцать первое" → 21, ordinal flag kept);
* digit strings for SNILS / phone / card numbers ("восемь девять ноль три" → "8903");
* dates: absolute ("четырнадцатого октября", "14.10"), relative ("послезавтра",
  "через два дня"), weekdays ("в следующий вторник") → ``datetime.date``;
* times: "в три часа дня", "пятнадцать тридцать", "15:30" → ``datetime.time``;
* day parts: "до обеда", "после обеда", "вечером" → ``morning|midday|afternoon|evening``;
* yes / no with negation handling ("да нет, не подходит" → no).

Everything here is pure, dependency-free and O(n) in the number of tokens, so it
can run in the call hot path (microseconds per utterance) and inside the
deterministic text→SQL automaton.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Iterable, Optional, Sequence, Union

# --------------------------------------------------------------------------------------
# Tokenisation
# --------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\d{1,2}[:]\d{2}|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?|\d+|[а-яa-z]+|[#*]", re.IGNORECASE)


_FILLERS = {"э", "ээ", "эм", "мм", "ну", "вот", "значит", "это", "типа", "короче", "пожалуйста", "пожалуйст"}


def tokenize(text: str, keep_fillers: bool = False) -> list[str]:
    """Lower-case, fold ё→е, split into words / numbers / clock tokens, drop speech fillers, repair ASR typos."""
    text = text.lower().replace("ё", "е")
    toks = _TOKEN_RE.findall(text)
    if not keep_fillers:
        toks = [t for t in toks if t not in _FILLERS]
    return [_repair(t) for t in toks]


def _edit1(a: str, b: str) -> bool:
    """True if ``a`` and ``b`` are within one substitution, insertion, deletion or adjacent transposition."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = 0
    skipped = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


_REPAIR_VOCAB: dict[int, list[str]] = {}


def _repair(tok: str) -> str:
    """Snap a long (≥6 letters) unknown word onto a weekday / month / number word one edit away."""
    if len(tok) < 6 or not tok.isalpha():
        return tok
    if not _REPAIR_VOCAB:
        vocab = set(_WEEKDAYS) | set(_MONTH_WORDS) | {w for w in _CARDINALS if len(w) >= 6} | set(_DAYPART_WORDS) | {"обеда", "половине", "сегодня", "завтра", "послезавтра", "следующий", "следующую", "следующей"}
        for w in vocab:
            _REPAIR_VOCAB.setdefault(len(w), []).append(w)
    if any(tok in ws for ws in _REPAIR_VOCAB.values()):
        return tok
    for length in (len(tok) - 1, len(tok), len(tok) + 1):
        for w in _REPAIR_VOCAB.get(length, ()):
            if tok[0] == w[0] and _edit1(tok, w):
                return w
    return tok


# --------------------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------------------

_CARDINALS: dict[str, int] = {}


def _add(value: int, *forms: str) -> None:
    for f in forms:
        _CARDINALS[f] = value


_add(0, "ноль", "нуль", "нулю", "ноля", "нуля")
_add(1, "один", "одна", "одно", "одного", "одной", "одну", "одним")
_add(2, "два", "две", "двух", "двум", "двумя")
_add(3, "три", "трех", "трем", "тремя")
_add(4, "четыре", "четырех", "четырем", "четырьмя")
_add(5, "пять", "пяти", "пятью")
_add(6, "шесть", "шести", "шестью")
_add(7, "семь", "семи", "семью")
_add(8, "восемь", "восьми", "восемью", "восьмью")
_add(9, "девять", "девяти", "девятью")
_add(10, "десять", "десяти", "десятью")
_add(11, "одиннадцать", "одиннадцати")
_add(12, "двенадцать", "двенадцати")
_add(13, "тринадцать", "тринадцати")
_add(14, "четырнадцать", "четырнадцати")
_add(15, "пятнадцать", "пятнадцати")
_add(16, "шестнадцать", "шестнадцати")
_add(17, "семнадцать", "семнадцати")
_add(18, "восемнадцать", "восемнадцати")
_add(19, "девятнадцать", "девятнадцати")
_add(20, "двадцать", "двадцати")
_add(30, "тридцать", "тридцати")
_add(40, "сорок", "сорока")
_add(50, "пятьдесят", "пятидесяти")
_add(60, "шестьдесят", "шестидесяти")
_add(70, "семьдесят", "семидесяти")
_add(80, "восемьдесят", "восьмидесяти")
_add(90, "девяносто", "девяноста")
_add(100, "сто", "ста")
_add(200, "двести", "двухсот")
_add(300, "триста", "трехсот")
_add(400, "четыреста", "четырехсот")
_add(500, "пятьсот", "пятисот")
_add(600, "шестьсот", "шестисот")
_add(700, "семьсот", "семисот")
_add(800, "восемьсот", "восьмисот")
_add(900, "девятьсот", "девятисот")
_add(1000, "тысяча", "тысячи", "тысяч", "тысячу")

# Ordinal stems, longest first so that "девятнадцат" wins over "девят".
_ORDINAL_STEMS: list[tuple[str, int]] = sorted(
    [
        ("перв", 1), ("втор", 2), ("трет", 3), ("четверт", 4), ("пят", 5), ("шест", 6),
        ("седьм", 7), ("восьм", 8), ("девят", 9), ("десят", 10), ("одиннадцат", 11),
        ("двенадцат", 12), ("тринадцат", 13), ("четырнадцат", 14), ("пятнадцат", 15),
        ("шестнадцат", 16), ("семнадцат", 17), ("восемнадцат", 18), ("девятнадцат", 19),
        ("двадцат", 20), ("тридцат", 30), ("сороков", 40), ("пятидесят", 50),
        ("шестидесят", 60), ("семидесят", 70), ("восьмидесят", 80), ("девяност", 90),
    ],
    key=lambda p: -len(p[0]),
)
_ORDINAL_SUFFIX_RE = re.compile(r"(ый|ой|ий|ое|ье|ая|ья|ого|ьего|ому|ьему|ым|ьим|ом|ьем|ую|ью|ые|ьи|ых|ьих)$")

_LITERAL_ORDINAL_SUFFIXES = {"го", "е", "ое", "ого", "й", "ый", "ой", "му", "ому"}

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTH_WORDS = {
    "января": 1, "январь": 1, "январе": 1, "февраля": 2, "февраль": 2, "феврале": 2,
    "марта": 3, "март": 3, "марте": 3, "апреля": 4, "апрель": 4, "апреле": 4,
    "мая": 5, "май": 5, "мае": 5, "июня": 6, "июнь": 6, "июне": 6, "июля": 7, "июль": 7, "июле": 7,
    "августа": 8, "август": 8, "августе": 8, "сентября": 9, "сентябрь": 9, "сентябре": 9,
    "октября": 10, "октябрь": 10, "октябре": 10, "ноября": 11, "ноябрь": 11, "ноябре": 11,
    "декабря": 12, "декабрь": 12, "декабре": 12,
}
_WEEKDAYS = {
    "понедельник": 0, "понедельника": 0, "понедельнику": 0,
    "вторник": 1, "вторника": 1, "вторнику": 1,
    "среда": 2, "среду": 2, "среды": 2, "среде": 2,
    "четверг": 3, "четверга": 3, "четвергу": 3,
    "пятница": 4, "пятницу": 4, "пятницы": 4, "пятнице": 4,
    "суббота": 5, "субботу": 5, "субботы": 5, "субботе": 5,
    "воскресенье": 6, "воскресенья": 6, "воскресенью": 6,
}
_RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_DAYPART_WORDS = {
    "утро": "morning", "утром": "morning", "утра": "morning", "утру": "morning",
    "обед": "midday", "обеда": "midday", "обедом": "midday", "обеду": "midday",
    "день": "midday", "днем": "midday", "дня": "midday",
    "вечер": "evening", "вечером": "evening", "вечера": "evening", "вечеру": "evening",
}

_DAYPART_PHRASES: tuple[tuple[str, str], ...] = (
    ("до обеда", "morning"), ("первой половине", "morning"), ("первую половину", "morning"),
    ("с утра", "morning"), ("пораньше", "morning"), ("после работы", "evening"),
    ("после обеда", "afternoon"), ("второй половине", "afternoon"), ("вторую половину", "afternoon"),
    ("попозже", "afternoon"), ("в обед", "midday"),
)

_YES = {
    "да", "ага", "угу", "конечно", "верно", "правильно", "подтверждаю", "подтвердить", "подтверждаем",
    "хорошо", "ладно", "ок", "окей", "подходит", "согласен", "согласна", "устраивает",
    "давайте", "давай", "точно", "именно", "разумеется", "оставьте", "оставляйте", "записывайте", "годится",
}
_NO = {
    "нет", "неа", "отмена", "отменить", "отмените", "отменяю", "неверно", "неправильно", "другой", "другое",
    "другую", "нельзя",
}
_NEGATION = {"не", "ни"}
_NO_PHRASES = ("не надо", "не нужно", "не подходит", "не устраивает", "не согласен", "не согласна",
               "не хочу", "не буду", "не могу", "не получится", "не смогу")

Item = Union[str, "Num"]


@dataclass(frozen=True)
class Num:
    value: int
    ordinal: bool = False
    literal: bool = False  # came from digits, not words
    width: int = 0  # number of spoken "digit groups" kept for digit strings

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


def _ordinal(tok: str) -> Optional[int]:
    if not _ORDINAL_SUFFIX_RE.search(tok):
        return None
    for stem, value in _ORDINAL_STEMS:
        if tok.startswith(stem):
            return value
    return None


def _magnitude(v: int) -> int:
    if v >= 1000:
        return 3
    if v >= 100:
        return 2
    if v >= 20:
        return 1
    return 0


def numberize(tokens: Sequence[str]) -> list[Item]:
    """Collapse runs of number words into ``Num`` items; keep other words."""
    items: list[Item] = []
    cur: Optional[int] = None
    cur_mag = -1
    cur_width = 0

    def flush() -> None:
        nonlocal cur, cur_mag, cur_width
        if cur is not None:
            items.append(Num(cur, False, False, cur_width))
        cur, cur_mag, cur_width = None, -1, 0

    for tok in tokens:
        if tok in _WEEKDAYS or tok in _MONTH_WORDS:
            flush()
            items.append(tok)
            continue
        if tok.isdigit():
            flush()
            items.append(Num(int(tok), False, True, len(tok)))
            continue
        if tok in _LITERAL_ORDINAL_SUFFIXES and cur is None and items and isinstance(items[-1], Num) and items[-1].literal:
            last = items.pop()
            items.append(Num(last.value, True, True, last.width))
            continue
        if tok in _CARDINALS:
            v = _CARDINALS[tok]
            mag = _magnitude(v)
            if cur is None:
                cur, cur_mag, cur_width = v, mag, 1
            elif v == 1000 and cur < 1000:
                cur, cur_mag, cur_width = cur * 1000, 3, cur_width + 1
            elif v != 0 and cur != 0 and mag < cur_mag and cur % (10 ** (mag + 1)) == 0:
                cur, cur_mag, cur_width = cur + v, mag, cur_width + 1
            else:
                flush()
                cur, cur_mag, cur_width = v, mag, 1
            continue
        ov = _ordinal(tok)
        if ov is not None:
            if cur is not None and cur != 0 and cur_mag >= 1 and ov < 10 and cur % 10 == 0:
                # "двадцать первое" → 21, "сто двадцать первое" → 121
                items.append(Num(cur + ov, True, False, cur_width + 1))
                cur, cur_mag, cur_width = None, -1, 0
            else:
                flush()
                items.append(Num(ov, True, False, 1))
            continue
        flush()
        items.append(tok)
    flush()
    return items


def words_to_number(text: str) -> Optional[int]:
    """Return the first number in ``text`` or ``None``."""
    for it in numberize(tokenize(text)):
        if isinstance(it, Num):
            return it.value
    return None


def extract_digits(text: str) -> str:
    """Concatenate all numbers into a digit string (phone / SNILS / card answers).

    "восемь девятьсот три пятьсот пятьдесят пять двенадцать тридцать четыре" →
    "89035551234".  Ordinals are ignored (they are never part of an ID).
    """
    out: list[str] = []
    for it in numberize(tokenize(text)):
        if isinstance(it, Num) and not it.ordinal:
            if it.literal:
                out.append(f"{it.value:0{it.width}d}")
            else:
                out.append(str(it.value))
    return "".join(out)


# --------------------------------------------------------------------------------------
# Yes / No
# --------------------------------------------------------------------------------------


def parse_yes_no(text: str) -> Optional[bool]:
    toks = tokenize(text)
    if not toks:
        return None
    joined = " ".join(toks)
    yes = no = 0
    first: Optional[bool] = None
    for i, tok in enumerate(toks):
        prev = toks[i - 1] if i else ""
        if tok in _NO:
            no += 1
            first = False if first is None else first
        elif tok in _YES:
            prev2 = toks[i - 2] if i >= 2 else ""
            if prev in _NEGATION or (prev2 in _NEGATION and tok != "да"):
                no += 1
                first = False if first is None else first
            elif tok == "да" and prev == "нет":
                continue  # "нет да" — noise after a clear no
            else:
                yes += 1
                first = True if first is None else first
    padded = f" {joined} "  # whole-word phrases only: "мне надо" must not match "не надо"
    for phrase in _NO_PHRASES:
        if f" {phrase} " in padded:
            no += 1
    if " да нет " in padded:
        return False
    if no and not yes:
        return False
    if yes and not no:
        return True
    if yes and no:
        # "да, нет, не подходит" → the explicit refusal wins; "нет, давайте" → ambiguous
        return False if first is True else None
    return None


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------


@dataclass
class DateSpan:
    start: int
    end: int  # exclusive, item indices
    value: date
    weekday_based: bool = False


def _next_weekday(today: date, wd: int, following: bool) -> date:
    delta = (wd - today.weekday()) % 7
    if delta == 0:
        delta = 7
    d = today + timedelta(days=delta)
    if following and d.isocalendar()[1] == today.isocalendar()[1] and d.year == today.year:
        d += timedelta(days=7)
    return d


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_dotted(tok: str, today: date) -> Optional[date]:
    kind, value = classify_dotted(tok, "", today)
    return value if kind == "date" else None


def classify_dotted(tok: str, prev: str, today: date) -> tuple[Optional[str], object]:
    """Decide whether "14.10" / "10.30" / "14.10.2026" is a date or a clock time."""
    parts = tok.split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts):
        return None, None
    a, b = int(parts[0]), int(parts[1])
    if len(parts) == 2 and a <= 23 and b <= 59:
        looks_like_time = b == 0 or b > 12 or (prev in ("в", "к") and b % 5 == 0)
        if looks_like_time and not (1 <= b <= 12 and prev in ("на",)):
            return "time", time(a, b)
    if not (1 <= a <= 31 and 1 <= b <= 12):
        return None, None
    year = today.year
    if len(parts) == 3:
        y = int(parts[2])
        year = y + 2000 if y < 100 else y
    val = _safe_date(year, b, a)
    if val is None:
        return None, None
    if len(parts) == 2 and val < today:
        val = _safe_date(year + 1, b, a) or val
    return "date", val


def find_dates(items: Sequence[Item], today: date, raw_tokens: Sequence[str] = ()) -> list[DateSpan]:
    spans: list[DateSpan] = []
    i = 0
    n = len(items)
    while i < n:
        it = items[i]
        if isinstance(it, str):
            if it in _RELATIVE_DAYS:
                spans.append(DateSpan(i, i + 1, today + timedelta(days=_RELATIVE_DAYS[it])))
                i += 1
                continue
            if it in _WEEKDAYS:
                following = i > 0 and isinstance(items[i - 1], str) and items[i - 1].startswith("следующ")
                start = i - 1 if following else i
                if start > 0 and isinstance(items[start - 1], str) and items[start - 1] in ("в", "во", "на"):
                    start -= 1
                spans.append(DateSpan(start, i + 1, _next_weekday(today, _WEEKDAYS[it], following), True))
                i += 1
                continue
            if it == "через" and i + 1 < n:
                nxt = items[i + 1]
                unit_idx = i + 2 if isinstance(nxt, Num) else i + 1
                count = nxt.value if isinstance(nxt, Num) else 1
                unit = items[unit_idx] if unit_idx < n and isinstance(items[unit_idx], str) else ""
                if unit.startswith("дн") or unit.startswith("день"):
                    spans.append(DateSpan(i, unit_idx + 1, today + timedelta(days=count)))
                    i = unit_idx + 1
                    continue
                if unit.startswith("недел"):
                    spans.append(DateSpan(i, unit_idx + 1, today + timedelta(days=7 * count)))
                    i = unit_idx + 1
                    continue
            i += 1
            continue
        # Num: day + month?  day alone (ordinal)?
        if 1 <= it.value <= 31:
            j = i + 1
            if j < n and isinstance(items[j], str) and items[j] in ("числа", "число"):
                j += 1
            if j < n and isinstance(items[j], str) and items[j] in _MONTH_WORDS:
                month = _MONTH_WORDS[items[j]]
                year = today.year
                end = j + 1
                if end < n and isinstance(items[end], Num) and not items[end].ordinal and 2000 <= items[end].value <= 2100:
                    year = items[end].value
                    end += 1
                    if end < n and isinstance(items[end], str) and items[end].startswith("год"):
                        end += 1
                val = _safe_date(year, month, it.value)
                if val is not None:
                    if val < today and year == today.year and (today - val).days > 60:
                        val = _safe_date(year + 1, month, it.value) or val
                    spans.append(DateSpan(i, end, val))
                    i = end
                    continue
            nxt_word = items[j] if j < n and isinstance(items[j], str) else ""
            prev_word = items[i - 1] if i > 0 and isinstance(items[i - 1], str) else ""
            if (it.ordinal or (j > i + 1)) and not nxt_word.startswith(("половин", "раз", "этаж", "кабинет", "окн", "подъезд")) \
                    and not prev_word.startswith(("половин", "пол")):
                # "четырнадцатого" / "пятого числа" → next such day of month
                val = _safe_date(today.year, today.month, it.value)
                if val is None or val < today:
                    m = today.month % 12 + 1
                    y = today.year + (1 if m == 1 else 0)
                    val = _safe_date(y, m, it.value)
                if val is not None:
                    spans.append(DateSpan(i, j, val))
                    i = j
                    continue
        i += 1
    return spans


def parse_date(text: str, today: date) -> Optional[date]:
    toks = tokenize(text)
    for idx, tok in enumerate(toks):
        if "." in tok:
            kind, value = classify_dotted(tok, toks[idx - 1] if idx else "", today)
            if kind == "date":
                return value
    spans = find_dates(numberize(toks), today)
    return spans[0].value if spans else None


# --------------------------------------------------------------------------------------
# Times & day parts
# --------------------------------------------------------------------------------------


@dataclass
class TimeSpan:
    start: int
    end: int
    value: time


_HOUR_WORDS = ("час", "часа", "часов", "часу")
_MINUTE_WORDS = ("минут", "минуты", "минута")


def _apply_meridiem(hour: int, part: Optional[str]) -> int:
    if part == "morning":
        return hour if hour < 12 else hour
    if part in ("midday", "afternoon") and hour < 12:
        return hour + 12 if hour <= 6 else hour  # "в три дня" → 15, "в одиннадцать дня" → 11
    if part == "evening" and hour < 12:
        return hour + 12
    if part is None and 1 <= hour <= 7:
        return hour + 12  # clinic works 08:00–20:00: "в три" means 15:00
    return hour


def find_times(items: Sequence[Item], date_spans: Sequence[DateSpan] = ()) -> list[TimeSpan]:
    covered = set()
    for ds in date_spans:
        covered.update(range(ds.start, ds.end))
    spans: list[TimeSpan] = []
    n = len(items)
    i = 0
    while i < n:
        it = items[i]
        if isinstance(it, str) and ":" in it:
            h, m = it.split(":")
            if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
                spans.append(TimeSpan(i, i + 1, time(int(h), int(m))))
            i += 1
            continue
        if isinstance(it, str) and ("." in it):
            prev = items[i - 1] if i > 0 and isinstance(items[i - 1], str) else ""
            kind, value = classify_dotted(it, prev, date(2000, 1, 1))
            if kind == "time" and i not in covered:
                spans.append(TimeSpan(i, i + 1, value))
            i += 1
            continue
        if isinstance(it, str) and it == "без" and i + 1 < n:
            # "без пятнадцати два" → 13:45, "без четверти три" → 14:45, "без десяти минут восемь" → 7:50 (→ 19:50 by clinic hours)
            j = i + 1
            if isinstance(items[j], str) and items[j] in ("четверти", "четверть"):
                minus = 15
            elif isinstance(items[j], Num) and not items[j].ordinal and 1 <= items[j].value <= 30:
                minus = items[j].value
            else:
                minus = 0
            nxt_is_hour = j + 1 < n and isinstance(items[j + 1], Num) and not items[j + 1].ordinal
            if isinstance(items[j], Num) and 21 <= items[j].value <= 29 and not nxt_is_hour:
                # numberize glued "двадцати пять" into 25: tens are the minutes, units the hour → 16:40
                end = j + 1
                part = None
                if end < n and isinstance(items[end], str) and items[end] in _DAYPART_WORDS:
                    part = _DAYPART_WORDS[items[end]]
                    end += 1
                hour = _apply_meridiem(items[j].value % 10, part) - 1
                spans.append(TimeSpan(i, end, time(hour % 24, 40)))
                i = end
                continue
            if minus:
                j += 1
                if j < n and isinstance(items[j], str) and items[j] in _MINUTE_WORDS:
                    j += 1
                if j < n and isinstance(items[j], Num) and not items[j].ordinal and 1 <= items[j].value <= 12 and j not in covered:
                    end = j + 1
                    if end < n and isinstance(items[end], str) and items[end] in _HOUR_WORDS:
                        end += 1
                    part = None
                    if end < n and isinstance(items[end], str) and items[end] in _DAYPART_WORDS:
                        part = _DAYPART_WORDS[items[end]]
                        end += 1
                    hour = _apply_meridiem(items[j].value, part) - 1
                    spans.append(TimeSpan(i, end, time(hour % 24, 60 - minus)))
                    i = end
                    continue
        if isinstance(it, str) and it in ("половина", "половине", "пол", "полчаса") and i + 1 < n and isinstance(items[i + 1], Num) and items[i + 1].ordinal:
            hour = items[i + 1].value - 1
            end = i + 2
            part = None
            if end < n and isinstance(items[end], str) and items[end] in _DAYPART_WORDS:
                part = _DAYPART_WORDS[items[end]]
                end += 1
            spans.append(TimeSpan(i, end, time(_apply_meridiem(hour, part) % 24, 30)))
            i = end
            continue
        if isinstance(it, Num) and not it.ordinal and i not in covered and 0 <= it.value <= 23:
            prev = items[i - 1] if i > 0 and isinstance(items[i - 1], str) else ""
            nxt = items[i + 1] if i + 1 < n else None
            has_hour_word = isinstance(nxt, str) and nxt in _HOUR_WORDS
            prep = prev in ("в", "во", "к", "на", "часов", "около", "после", "до", "с")
            if not (has_hour_word or prep):
                i += 1
                continue
            end = i + 1
            if has_hour_word:
                end += 1
            minute = 0
            if end < n and end not in covered and isinstance(items[end], Num) and not items[end].ordinal and 0 <= items[end].value <= 59:
                # "пятнадцать тридцать", "три часа двадцать минут"
                cand = items[end].value
                after = items[end + 1] if end + 1 < n else None
                minute_word = isinstance(after, str) and after in _MINUTE_WORDS
                if has_hour_word or minute_word or cand >= 10 or (items[end].literal and items[end].width == 2):
                    minute = cand
                    end += 1
                    if minute_word:
                        end += 1
            part = None
            if end < n and isinstance(items[end], str) and items[end] in _DAYPART_WORDS:
                part = _DAYPART_WORDS[items[end]]
                end += 1
            hour = _apply_meridiem(it.value, part)
            if hour > 23:
                i += 1
                continue
            spans.append(TimeSpan(i, end, time(hour, minute)))
            i = end
            continue
        if isinstance(it, str) and it == "час" and i > 0 and items[i - 1] in ("в", "к"):
            spans.append(TimeSpan(i, i + 1, time(13, 0)))
            i += 1
            continue
        i += 1
    return spans


def parse_time(text: str) -> Optional[time]:
    toks = tokenize(text)
    items = numberize(toks)
    spans = find_times(items, find_dates(items, date(2000, 1, 1)))
    return spans[0].value if spans else None


def parse_daypart(text: str) -> Optional[str]:
    """morning / midday / afternoon / evening, else None (phrases win over single words)."""
    toks = tokenize(text)
    joined = " ".join(toks)
    for phrase, part in _DAYPART_PHRASES:
        if phrase in joined:
            return part
    for tok in toks:
        if tok in _DAYPART_WORDS:
            return _DAYPART_WORDS[tok]
    return None


# --------------------------------------------------------------------------------------
# Whole-utterance interpretation
# --------------------------------------------------------------------------------------


@dataclass
class Interpretation:
    text: str
    tokens: list[str]
    items: list[Item]
    normalized: str
    numbers: list[int] = field(default_factory=list)
    digits: str = ""
    yes_no: Optional[bool] = None
    date: Optional[date] = None
    weekday_based: bool = False
    time: Optional[time] = None
    daypart: Optional[str] = None
    ordinal_days: list[int] = field(default_factory=list)

    @property
    def has_date(self) -> bool:
        return self.date is not None

    @property
    def has_time(self) -> bool:
        return self.time is not None


def interpret(text: str, today: Optional[date] = None) -> Interpretation:
    today = today or date.today()
    toks = tokenize(text)
    items = numberize(toks)
    # dotted dates are tokens like "14.10"
    date_spans = find_dates(items, today)
    for idx, it in enumerate(items):
        if isinstance(it, str) and "." in it:
            prev = items[idx - 1] if idx > 0 and isinstance(items[idx - 1], str) else ""
            kind, value = classify_dotted(it, prev, today)
            if kind == "date" and not any(s.start <= idx < s.end for s in date_spans):
                date_spans.append(DateSpan(idx, idx + 1, value))
    date_spans.sort(key=lambda s: s.start)
    time_spans = find_times(items, date_spans)
    covered: dict[int, tuple[int, str]] = {}
    for ds in date_spans:
        covered[ds.start] = (ds.end, ds.value.isoformat())
    for ts in time_spans:
        if ts.start in covered:
            continue
        covered[ts.start] = (ts.end, ts.value.strftime("%H:%M"))
    out: list[str] = []
    i = 0
    while i < len(items):
        if i in covered:
            end, rep = covered[i]
            out.append(rep)
            i = max(end, i + 1)
            continue
        it = items[i]
        out.append(str(it.value) if isinstance(it, Num) else it)
        i += 1
    normalized = " ".join(out)
    consumed = {idx for ts in time_spans for idx in range(ts.start, ts.end)}
    consumed.update(idx for ds in date_spans for idx in range(ds.start, ds.end))
    joined = " ".join(toks)
    daypart = None
    for phrase, part in _DAYPART_PHRASES:
        if phrase in joined:
            daypart = part
            break
    if daypart is None:
        for idx, it in enumerate(items):
            if isinstance(it, str) and it in _DAYPART_WORDS and idx not in consumed:
                daypart = _DAYPART_WORDS[it]
                break
    return Interpretation(
        text=text,
        tokens=toks,
        items=items,
        normalized=normalized,
        numbers=[it.value for it in items if isinstance(it, Num)],
        digits=extract_digits(text),
        yes_no=parse_yes_no(text),
        date=date_spans[0].value if date_spans else None,
        weekday_based=bool(date_spans and date_spans[0].weekday_based),
        time=time_spans[0].value if time_spans else None,
        daypart=daypart,
        ordinal_days=[it.value for it in items if isinstance(it, Num) and it.ordinal],
    )


def canonicalize(text: str, today: Optional[date] = None) -> str:
    """Return the utterance with numbers, dates and times in canonical form."""
    return interpret(text, today).normalized


def normalize_many(texts: Iterable[str], today: Optional[date] = None) -> list[str]:
    return [canonicalize(t, today) for t in texts]

"""Русские даты, время и разбор естественных формулировок.

Регламент (3.2) требует: даты называются с днём недели («в четверг, первого
октября»), время — словами («в девять тридцать»). Здесь обе стороны:
разбор сказанного пациентом и обратный синтез для TTS.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional

# --------------------------------------------------------------------------
# Словари
# --------------------------------------------------------------------------

WEEKDAYS_NOM = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_ACC = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# Порядковые числительные в родительном падеже: «первого октября»
_ORDINALS_GEN = {
    1: "первого", 2: "второго", 3: "третьего", 4: "четвертого", 5: "пятого",
    6: "шестого", 7: "седьмого", 8: "восьмого", 9: "девятого", 10: "десятого",
    11: "одиннадцатого", 12: "двенадцатого", 13: "тринадцатого", 14: "четырнадцатого",
    15: "пятнадцатого", 16: "шестнадцатого", 17: "семнадцатого", 18: "восемнадцатого",
    19: "девятнадцатого", 20: "двадцатого", 21: "двадцать первого", 22: "двадцать второго",
    23: "двадцать третьего", 24: "двадцать четвертого", 25: "двадцать пятого",
    26: "двадцать шестого", 27: "двадцать седьмого", 28: "двадцать восьмого",
    29: "двадцать девятого", 30: "тридцатого", 31: "тридцать первого",
}

MONTH_STEMS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "май": 5, "мая": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12, "ма": 5,
}

MONTH_RE = "|".join(sorted((re.escape(s) for s in MONTH_STEMS), key=len, reverse=True))

# Числительные 0..31 (именительный и родительный падежи). Определены до сборки
# регулярных выражений ниже, потому что участвуют в них.
_NUM = {
    "ноль": 0, "нуль": 0, "один": 1, "одна": 1, "одно": 1, "первое": 1, "первого": 1,
    "два": 2, "две": 2, "второе": 2, "второго": 2,
    "три": 3, "третье": 3, "третьего": 3,
    "четыре": 4, "четвертое": 4, "четвертого": 4,
    "пять": 5, "пятое": 5, "пятого": 5,
    "шесть": 6, "шестое": 6, "шестого": 6,
    "семь": 7, "седьмое": 7, "седьмого": 7,
    "восемь": 8, "восьмое": 8, "восьмого": 8,
    "девять": 9, "девятое": 9, "девятого": 9,
    "десять": 10, "десятое": 10, "десятого": 10,
    "одиннадцать": 11, "одиннадцатое": 11, "одиннадцатого": 11,
    "двенадцать": 12, "двенадцатое": 12, "двенадцатого": 12,
    "тринадцать": 13, "тринадцатое": 13, "тринадцатого": 13,
    "четырнадцать": 14, "четырнадцатое": 14, "четырнадцатого": 14,
    "пятнадцать": 15, "пятнадцатое": 15, "пятнадцатого": 15,
    "шестнадцать": 16, "шестнадцатое": 16, "шестнадцатого": 16,
    "семнадцать": 17, "семнадцатое": 17, "семнадцатого": 17,
    "восемнадцать": 18, "восемнадцатое": 18, "восемнадцатого": 18,
    "девятнадцать": 19, "девятнадцатое": 19, "девятнадцатого": 19,
    "двадцать": 20, "двадцатое": 20, "двадцатого": 20,
    "тридцать": 30, "тридцатое": 30, "тридцатого": 30,
    "тридцать первое": 31, "тридцать первого": 31,
}
_YEAR_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
              "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90}
_YEAR_ONES = {"первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5,
              "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9, "ноль": 0,
              "нулевого": 0}
# Родительный падеж десятков: «девяностого года» → 90
_YEAR_TENS_GEN = {
    "двадцатого": 20, "тридцатого": 30, "сорокового": 40, "пятидесятого": 50,
    "шестидесятого": 60, "семидесятого": 70, "восьмидесятого": 80, "девяностого": 90,
}

_DAY_WORDS = "|".join(sorted((re.escape(w) for w in _NUM if " " not in w), key=len, reverse=True))
# «двенадцатое марта», «12 марта», «12 марта 1978 года». Год НЕ входит в это
# выражение: его подбираем отдельно по словарю годовых числительных, иначе
# «пятое мая шестьдесят второго» теряет день и превращается в 1905 год.
_DATE_WORDS_RE = re.compile(
    rf"(?<![а-я])(?P<day>\d{{1,2}}|{_DAY_WORDS})\s+(?P<month>{MONTH_RE})[а-я]*",
)
_RE_MONTH_FIRST_RE = re.compile(rf"(?P<month>{MONTH_RE})[а-я]*\s+(?P<day>\d{{1,2}})")

# Слова, из которых собирается год: «семьдесят восьмого», «две тысячи девятнадцатого»
_YEAR_WORDS = set(_YEAR_TENS) | set(_YEAR_ONES)
_YEAR_WORDS |= {"тысячи", "тысяч", "две", "двух", "девятнадцатого", "двадцатого"}
_YEAR_START = sorted(
    (w for w in _YEAR_WORDS if w not in _NUM or _NUM[w] >= 20),
    key=len, reverse=True,
)


def _strip_explicit_date(t: str) -> str:
    """Убрать уже разобранную ДД.ММ.ГГГГ, чтобы она не мешала словам."""
    return RE_DMY.sub(" ", t, count=1)


def _year_from_words(words: list[str]) -> Optional[int]:
    """Собрать год из слов: «семьдесят восьмого» → 1978, «девяностого» → 1990."""
    if not words:
        return None
    if words[0] in ("две", "двух") and len(words) > 1 and words[1].startswith("тысяч"):
        # «две тысячи девятнадцатого» → 2019
        rest = 0
        for word in words[2:]:
            part = _NUM_NUMERIC_EXTRA.get(word) or _YEAR_TENS.get(word) or _YEAR_TENS_GEN.get(word)
            if part:
                rest += part
            elif word in _YEAR_ONES:
                rest += _YEAR_ONES[word]
        return 2000 + rest
    if words[0] in ("тысяча", "тысячи", "тысяч"):
        rest = _words_to_number(words[1:])
        return _fix_year(rest) if rest else None
    head = _YEAR_TENS_GEN.get(words[0]) or _YEAR_TENS.get(words[0])
    if head is None:
        return None
    value = head
    if len(words) > 1:
        tail = _YEAR_ONES.get(words[1])
        if tail and head % 10 == 0:
            value = head + tail
    return 1900 + value if value < 100 else _fix_year(value)


_NUM_NUMERIC_EXTRA = {"десятого": 10, "одиннадцатого": 11, "двенадцатого": 12,
                      "тринадцатого": 13, "четырнадцатого": 14, "пятнадцатого": 15,
                      "шестнадцатого": 16, "семнадцатого": 17, "восемнадцатого": 18,
                      "девятнадцатого": 19, "двадцатого": 20}


def _extract_year_words(t: str, start: int) -> Optional[int]:
    """Собрать год из слов, начиная с позиции ``start``.

    Возвращает None, если года в тексте нет: тогда дата считается днём
    рождения без года и приводится к ближайшему прошедшему числу.
    """
    tail = t[start:start + 60].strip(" ,.")
    if not tail:
        return None
    words = tail.split()
    if not words:
        return None
    if words[0].isdigit():
        return _fix_year(int(words[0]))
    known = set(_YEAR_TENS_GEN) | set(_YEAR_TENS) | {"две", "двух", "тысяча", "тысячи", "тысяч"}
    if words[0] not in known:
        return None
    # «две тысячи девятнадцатого» — три слова, поэтому берём запас
    return _year_from_words(words[:4])


def _parse_year_words(raw: str) -> Optional[int]:
    """«семьдесят восьмого» → 1978, «две тысячи девятнадцатого» → 2019."""
    words = raw.replace("-", " ").split()
    if not words:
        return None
    if words[0] in ("две", "двух") and len(words) > 1 and words[1].startswith("тысяч"):
        rest = _words_to_number(words[2:])
        return 2000 + (rest or 0)
    value = _words_to_number(words)
    if value is None:
        return None
    if value < 20 and len(words) == 1:
        return 1900 + value
    if value < 100:
        return 1900 + value
    return _fix_year(value)


def _day_from_word(raw: str) -> int:
    return _words_to_number([raw]) or 0


def _nearest_birthday(day: int, month: int, today: date) -> Optional[date]:
    """Без года — ближайший день рождения в прошлом (Приложение А.3)."""
    for year in (today.year, today.year - 1):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None
# Числительные уже определены выше (нужны для сборки регулярных выражений).

# Специальности: канонично → синонимы (Приложение А.1)
SPECIALTY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "терапевт": ("терапевт", "терапевта", "терапевту", "участковый", "участкового",
                 "свой врач", "к врачу", "к доктору", "общий врач", "терапевтша",
                 "терапевтическое"),
    "педиатр": ("педиатр", "педиатра", "педиатру", "детский врач", "детского врача",
                "участковый педиатр", "с ребёнком", "с ребенком"),
    "оториноларинголог": ("лор", "лора", "лору", "лором", "ухо-горло-нос", "ушник",
                          "отоларинголог", "отоларинголога", "оториноларинголог"),
    "офтальмолог": ("офтальмолог", "офтальмолога", "окулист", "окулиста", "глазной",
                    "глазного", "глазник", "проверить зрение", "зрение"),
    "акушер-гинеколог": ("гинеколог", "гинеколога", "гинекологу", "женский врач",
                         "женскую консультацию", "женской консультации", "акушер"),
    "хирург": ("хирург", "хирурга", "хирургу", "вскрыть", "удалить родинку",
               "вросший ноготь"),
    "невролог": ("невролог", "невролога", "неврологу", "невропатолог", "нервный врач"),
    "кардиолог": ("кардиолог", "кардиолога", "кардиологу", "сердечный врач", "по сердцу"),
    "эндокринолог": ("эндокринолог", "эндокринолога", "эндокринологу", "по щитовидке",
                     "по сахару", "щитовидк"),
    "уролог": ("уролог", "уролога", "урологу", "мужской врач"),
    "гастроэнтеролог": ("гастроэнтеролог", "гастроэнтеролога", "гастроэнтерологу",
                        "желудок", "жкт"),
    "пульмонолог": ("пульмонолог", "пульмонолога", "пульмонологу"),
    "дерматовенеролог": ("дерматовенеролог", "дерматолог", "дерматолога", "кожа",
                         "сыпь"),
    "травматолог-ортопед": ("травматолог", "травматолога", "ортопед", "ортопеда"),
    "стоматолог": ("стоматолог", "стоматолога", "зубной", "к зубному", "дантист",
                   "зуб болит", "зуб", "запломбировать", "стоматологию"),
    "психиатр": ("психиатр", "психиатра", "психотерапевт"),
    "онколог": ("онколог", "онколога"),
    "инфекционист": ("инфекционист", "инфекциониста"),
    "нефролог": ("нефролог", "нефролога"),
    "гематолог": ("гематолог", "гематолога"),
    "ревматолог": ("ревматолог", "ревматолога"),
    "аллерголог": ("аллерголог", "аллерголога", "аллергия"),
}

_SPECIALTY_INDEX: dict[str, str] = {}
for _canon, _syns in SPECIALTY_SYNONYMS.items():
    for _s in _syns:
        _SPECIALTY_INDEX[_s] = _canon
    _SPECIALTY_INDEX[_canon] = _canon


# --------------------------------------------------------------------------
# Нормализация
# --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Регистр, ё→е, типографские кавычки, сжатие пробелов."""
    if not text:
        return ""
    t = text.lower().replace("ё", "е").replace("—", " ").replace("–", " ")
    t = t.replace("«", " ").replace("»", " ").replace('"', " ")
    t = re.sub(r"[^\w\s:.,\-/+()]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------
# Разбор дат
# --------------------------------------------------------------------------

RE_DMY = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})\b")
RE_DM = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})\b")


def _fix_year(y: int) -> int:
    """Год из двух цифр: пациенту не может быть больше 110 лет (А.3)."""
    if y >= 1000:
        return y
    current = date.today().year
    century = current // 100 * 100
    candidate = century + y
    if candidate > current:
        candidate -= 100
    return candidate


def _words_to_number(words: list[str]) -> Optional[int]:
    """«двадцать пять» → 25, «семьдесят восьмого» → 78 (для года)."""
    total = 0
    found = False
    for w in words:
        w = w.strip()
        if w in _NUM:
            total += _NUM[w]
            found = True
        elif w in _YEAR_TENS:
            total += _YEAR_TENS[w]
            found = True
    return total if found else None


def parse_date_ru(text: str, today: Optional[date] = None) -> Optional[date]:
    """Разобрать дату рождения или дату визита из реплики пациента.

    Поддерживает: 12.03.1978, 12.03.78, «двенадцатое марта семьдесят восьмого»,
    «12 марта 1978 года», а также относительные «завтра», «послезавтра»,
    «в четверг», «на следующей неделе», «через неделю» (Приложение А.3).
    """
    if not text:
        return None
    today = today or date.today()
    t = normalize(text)

    # 12.03.1978 / 12.03.78 / 12-03-78
    m = RE_DMY.search(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), _fix_year(int(m.group(3)))
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    # «двенадцатое марта семьдесят восьмого» / «12 марта 1978 года»
    match = _DATE_WORDS_RE.search(t)
    if match is None:
        match = _RE_MONTH_FIRST_RE.search(t)
    if match:
        day_raw = match.group("day")
        day = int(day_raw) if day_raw.isdigit() else _day_from_word(day_raw)
        month = None
        for stem, number in MONTH_STEMS.items():
            if match.group("month").startswith(stem):
                month = number
                break
        if day and month and 1 <= day <= 31:
            # Год — только если сразу после месяца стоит годовое числительное
            after = _strip_explicit_date(t)[match.end():].lstrip(" ,.")
            after = re.sub(r"^года\s+", "", after)
            year = _extract_year_words(after, 0) if after else None
            if year:
                try:
                    return date(_fix_year(year), month, day)
                except ValueError:
                    return None
            return _nearest_birthday(day, month, today)

    # Относительные даты
    if "послезавтра" in t:
        return today + timedelta(days=2)
    if "завтра" in t:
        return today + timedelta(days=1)
    if "сегодня" in t or "сейчас" in t:
        return today
    if "через две недели" in t:
        return today + timedelta(days=14)
    if "через неделю" in t:
        return today + timedelta(days=7)
    if "на следующей неделе" in t:
        return today + timedelta(days=7 - today.weekday())

    # «в четверг» — ближайший будущий (Приложение А.3)
    for idx, name in enumerate(WEEKDAYS_NOM):
        acc = WEEKDAYS_ACC[idx]
        if re.search(rf"\b(в|во|к|на)\s+{name}", t) or re.search(rf"\b{acc}\b", t):
            delta = (idx - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)
    return None


def parse_birth_date(text: str) -> Optional[date]:
    """Дата рождения — только абсолютная, без «завтра»."""
    if not text:
        return None
    t = normalize(text)
    if RE_DMY.search(t):
        return parse_date_ru(text)
    for stem in MONTH_STEMS:
        if stem in t:
            return parse_date_ru(text)
    return None


# --------------------------------------------------------------------------
# Разбор времени
# --------------------------------------------------------------------------

RE_HM = re.compile(r"\b([01]?\d|2[0-3])[:.\-]([0-5]\d)\b")


_MIN_TENS = {"десять": 10, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
_MIN_ONES = {"пять": 5, "десять": 10, "пятнадцать": 15, "двадцать": 20, "двадцать пять": 25,
             "тридцать": 30, "тридцать пять": 35, "сорок": 40, "сорок пять": 45,
             "пятьдесят": 50, "пятьдесят пять": 55}


def _minute_from_words(first: str, second: str | None) -> int:
    """«сорок» → 40, «сорок пять» → 45, «ноль-ноль» → 0."""
    if first in ("ноль-ноль", "ноль", "нуль", "ровно", "ноль-ноль"):
        return 0
    base = _MIN_TENS.get(first) or _MIN_ONES.get(first) or _words_to_number([first]) or 0
    if second and second in _NUM_NUMERIC:
        extra = _NUM_NUMERIC[second]
        if extra < 10 and base >= 10:
            return base + extra
    return base


_NUM_NUMERIC = {
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "пятнадцать": 15, "двадцать": 20, "двадцать пять": 25,
    "тридцать": 30, "тридцать пять": 35, "сорок": 40, "сорок пять": 45,
    "пятьдесят": 50,
}
_NUM_NUMERIC.update({w: v for w, v in _NUM.items() if v < 10})
_MINUTE_FIRST = sorted(_MIN_TENS, key=len, reverse=True)
_SECOND_WORDS = sorted((w for w in _NUM_NUMERIC if w != "сорок"), key=len, reverse=True)

_TIME_WORDS_RE = re.compile(
    r"(?:в|к|около|на)\s+(?P<hour>[а-я]+)"
    r"(?:\s+(?P<min1>[а-я\-]+)(?:\s+(?P<min2>[а-я]+))?)?"
)


def parse_time_ru(text: str) -> Optional[time]:
    """«в девять тридцать», «9:30», «после обеда», «с утра»."""
    if not text:
        return None
    t = normalize(text)
    m = RE_HM.search(t)
    if m:
        return time(int(m.group(1)), int(m.group(2)))
    m = _TIME_WORDS_RE.search(t)
    if m:
        hour_word = m.group("hour")
        hour = _NUM.get(hour_word)
        if hour is not None and 0 <= hour <= 23:
            minute = 0
            min1, min2 = m.group("min1"), m.group("min2")
            if min1:
                # Отбрасываем слова, которые не являются минутами
                # («подождите», «пожалуйста», «сегодня»)
                minute = _minute_from_words(min1, min2)
                if minute is None or minute > 59:
                    minute = 0
            return time(hour, int(minute))
    return None


def part_of_day(text: str) -> Optional[str]:
    """Часть дня по Приложению А.3: утро 7:30–12:00, день 12:00–16:00, вечер после 16:00."""
    t = normalize(text)
    if any(w in t for w in ("утром", "с утра", "пораньше", "утро", "утренн")):
        return "morning"
    if any(w in t for w in ("после обеда", "днем", "днём", "дневн", "в обед", "обед")):
        return "afternoon"
    if any(w in t for w in ("вечером", "вечер", "вечерн", "попозже", "позже")):
        return "evening"
    return None


def parse_weekday_ban(text: str) -> tuple[int, ...]:
    """«не в пятницу», «только по вторникам» → дни недели для исключения."""
    t = normalize(text)
    banned: list[int] = []
    for idx, name in enumerate(WEEKDAYS_NOM):
        acc = WEEKDAYS_ACC[idx]
        if re.search(rf"(не|кроме|исключая)[^.]{{0,12}}\b(в\s+)?({name}|{acc})", t):
            banned.append(idx)
    return tuple(banned)


# --------------------------------------------------------------------------
# Синтез для TTS и подсказок
# --------------------------------------------------------------------------

_HOUR_WORDS = {
    0: "ноль", 1: "час", 2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть",
    7: "семь", 8: "восемь", 9: "девять", 10: "десять", 11: "одиннадцать",
    12: "двенадцать", 13: "тринадцать", 14: "четырнадцать", 15: "пятнадцать",
    16: "шестнадцать", 17: "семнадцать", 18: "восемнадцать", 19: "девятнадцать",
    20: "двадцать", 21: "двадцать один", 22: "двадцать два", 23: "двадцать три",
}


_HOUR_ONLY = {1: "час", 2: "два", 3: "три", 4: "четыре"}
_MINUTE_WORDS = {
    0: "ноль-ноль", 5: "пять", 10: "десять", 15: "пятнадцать", 20: "двадцать",
    25: "двадцать пять", 30: "тридцать", 35: "тридцать пять", 40: "сорок",
    45: "сорок пять", 50: "пятьдесят", 55: "пятьдесят пять",
}


def time_words(dt: datetime) -> str:
    """«в восемь сорок пять», «в пятнадцать ноль-ноль» (регламент 3.2)."""
    h, m = dt.hour, dt.minute
    hour = _HOUR_WORDS.get(h, str(h))
    if m == 0:
        return f"в {hour} ноль-ноль"
    if m in _MINUTE_WORDS:
        return f"в {hour} {_MINUTE_WORDS[m]}"
    if m < 10:
        return f"в {hour} ноль {_NUM_WORD_ONES.get(m, str(m))}"
    return f"в {hour} {_NUM_WORD_TENS.get(m // 10, '')} {_NUM_WORD_ONES.get(m % 10, '')}".strip()


_NUM_WORD_ONES = {1: "одна", 2: "две", 3: "три", 4: "четыре", 5: "пять",
                  6: "шесть", 7: "семь", 8: "восемь", 9: "девять"}
_NUM_WORD_TENS = {2: "двадцать", 3: "тридцать", 4: "сорок", 5: "пятьдесят"}


def date_words(d: date, today: Optional[date] = None) -> str:
    """«в четверг, первого октября», «сегодня, в среду, тридцатое сентября»."""
    today = today or date.today()
    wd = WEEKDAYS_ACC[d.weekday()]
    preposition = "во" if wd.startswith("в") else "в"
    day_word = _ORDINALS_GEN.get(d.day, str(d.day))
    month = MONTHS_GEN[d.month - 1]
    if d == today:
        return f"сегодня, {preposition} {wd}, {day_word} {month}"
    if d == today + timedelta(days=1):
        return f"завтра, {preposition} {wd}, {day_word} {month}"
    return f"{preposition} {wd}, {day_word} {month}"


def short_date(d: date) -> str:
    """02.10 — как в SMS-шаблонах."""
    return d.strftime("%d.%m")


_ORDINALS_NEUT = {
    1: "первое", 2: "второе", 3: "третье", 4: "четвертое", 5: "пятое", 6: "шестое",
    7: "седьмое", 8: "восьмое", 9: "девятое", 10: "десятое", 11: "одиннадцатое",
    12: "двенадцатое", 13: "тринадцатое", 14: "четырнадцатое", 15: "пятнадцатое",
    16: "шестнадцатое", 17: "семнадцатое", 18: "восемнадцатое", 19: "девятнадцатое",
    20: "двадцатое", 21: "двадцать первое", 22: "двадцать второе", 23: "двадцать третье",
    24: "двадцать четвертое", 25: "двадцать пятое", 26: "двадцать шестое",
    27: "двадцать седьмое", 28: "двадцать восьмое", 29: "двадцать девятое",
    30: "тридцатое", 31: "тридцать первое",
}

_YEAR_TENS_WORDS = {20: "двадцать", 30: "тридцать", 40: "сорок", 50: "пятьдесят",
                    60: "шестьдесят", 70: "семьдесят", 80: "восемьдесят", 90: "девяносто"}
_YEAR_ONES_GEN = {0: "нулевого", 1: "первого", 2: "второго", 3: "третьего",
                  4: "четвертого", 5: "пятого", 6: "шестого", 7: "седьмого",
                  8: "восьмого", 9: "девятого"}


_NUM_GEN_TEENS = {10: "десятого", 11: "одиннадцатого", 12: "двенадцатого",
                  13: "тринадцатого", 14: "четырнадцатого", 15: "пятнадцатого",
                  16: "шестнадцатого", 17: "семнадцатого", 18: "восемнадцатого",
                  19: "девятнадцатого"}


def year_genitive(year: int) -> str:
    """1978 → «семьдесят восьмого» — как пациент называет год рождения."""
    tail = year % 100
    tens, ones = divmod(tail, 10)
    if tens == 1:
        return _NUM_GEN_TEENS.get(tail, str(tail))
    parts = []
    if tens:
        parts.append(_YEAR_TENS_WORDS.get(tens, str(tens)))
    parts.append(_YEAR_ONES_GEN.get(ones, ""))
    return " ".join(p for p in parts if p)


def month_date_words(iso_or_dmy: str) -> str:
    """«12.03.1978» → «двенадцатое марта семьдесят восьмого» (реплика пациента)."""
    m = re.match(r"\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", iso_or_dmy or "")
    if not m:
        return iso_or_dmy or ""
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year = _fix_year(year)
    month_word = MONTHS_GEN[month - 1] if 1 <= month <= 12 else ""
    day_word = _ORDINALS_NEUT.get(day, str(day))
    return f"{day_word} {month_word} {year_genitive(year)}".strip()


def digits_to_words(value: int) -> str:
    """Число словами — для цен и коротких чисел."""
    units = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять",
             "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
             "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
    tens = {2: "двадцать", 3: "тридцать", 4: "сорок", 5: "пятьдесят", 6: "шестьдесят",
            7: "семьдесят", 8: "восемьдесят", 9: "девяносто"}
    if value < 20:
        return units[value]
    if value < 100:
        t, o = divmod(value, 10)
        return tens[t] + (" " + units[o] if o else "")
    if value < 1000:
        h, rest = divmod(value, 100)
        head = units[h] + " сот" + ("ня" if h in (3, 4) else ("ен" if h in (5, 6, 7, 8, 9) else "о"))
        return (head + (" " + digits_to_words(rest) if rest else "")).strip()
    th, rest = divmod(value, 1000)
    head = digits_to_words(th) + " тысяч" + ("а" if th in (2, 3, 4) else "")
    return (head + (" " + digits_to_words(rest) if rest else "")).strip()


__all__ = [
    "WEEKDAYS_NOM", "WEEKDAYS_ACC", "WEEKDAYS_SHORT", "MONTHS_GEN",
    "SPECIALTY_SYNONYMS", "normalize", "parse_date_ru", "parse_birth_date",
    "parse_time_ru", "part_of_day", "parse_weekday_ban", "time_words",
    "date_words", "short_date", "digits_to_words", "month_date_words",
    "year_genitive",
]

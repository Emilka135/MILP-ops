"""Numbers, dates and times → Russian words for TTS (the inverse of nlu.normalize).

Silero reads digits inconsistently ("14:30" may come out as "четырнадцать
тридцать" or "четырнадцать двоеточие тридцать"), so prompts are verbalised
deterministically before synthesis: ``date → "пятнадцатого октября, в среду"``,
``time → "четырнадцать тридцать"``, digit strings → digit-by-digit.
"""

from __future__ import annotations

from datetime import date, time

_UNITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять", "одиннадцать",
          "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_UNITS_F = {1: "одна", 2: "две"}
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
_ORD_GEN = {  # genitive ordinals for dates: "первого", "двадцать первого"
    1: "первого", 2: "второго", 3: "третьего", 4: "четвёртого", 5: "пятого", 6: "шестого", 7: "седьмого", 8: "восьмого",
    9: "девятого", 10: "десятого", 11: "одиннадцатого", 12: "двенадцатого", 13: "тринадцатого", 14: "четырнадцатого",
    15: "пятнадцатого", 16: "шестнадцатого", 17: "семнадцатого", 18: "восемнадцатого", 19: "девятнадцатого", 20: "двадцатого",
    30: "тридцатого",
}
_MONTH_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_WEEKDAY_ACC = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье"]
WEEKDAY_NOM = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def number_to_words(n: int, feminine: bool = False) -> str:
    if n < 0:
        return "минус " + number_to_words(-n, feminine)
    if n < 20:
        return _UNITS_F[n] if feminine and n in _UNITS_F else _UNITS[n]
    if n < 100:
        t, u = divmod(n, 10)
        return _TENS[t] + ("" if u == 0 else " " + number_to_words(u, feminine))
    if n < 1000:
        h, r = divmod(n, 100)
        return _HUNDREDS[h] + ("" if r == 0 else " " + number_to_words(r, feminine))
    if n < 1_000_000:
        k, r = divmod(n, 1000)
        word = "тысяча" if k % 10 == 1 and k % 100 != 11 else "тысячи" if 2 <= k % 10 <= 4 and not 12 <= k % 100 <= 14 else "тысяч"
        head = number_to_words(k, feminine=True) + " " + word
        return head + ("" if r == 0 else " " + number_to_words(r, feminine))
    return str(n)


def day_ordinal_genitive(d: int) -> str:
    if d in _ORD_GEN:
        return _ORD_GEN[d]
    t, u = divmod(d, 10)
    return _TENS[t] + " " + _ORD_GEN[u]


def date_to_words(d: date, with_weekday: bool = True, with_year: bool = False) -> str:
    s = f"{day_ordinal_genitive(d.day)} {_MONTH_GEN[d.month]}"
    if with_year:
        s += f" {number_to_words(d.year)} года"
    if with_weekday:
        s += f", {_WEEKDAY_ACC[d.weekday()]}"
    return s


def time_to_words(t: time) -> str:
    h = number_to_words(t.hour)
    if t.minute == 0:
        hw = "час" if t.hour in (1, 21) else "часа" if t.hour % 10 in (2, 3, 4) and not 12 <= t.hour <= 14 else "часов"
        if t.hour == 0:
            return "ноль часов"
        return f"{h} {hw}" if t.hour < 10 else f"{h} ноль ноль"
    m = number_to_words(t.minute)
    if t.minute < 10:
        m = "ноль " + m
    return f"{h} {m}"


def digits_to_words(s: str, group: int = 0) -> str:
    words = [_UNITS[int(ch)] for ch in s if ch.isdigit()]
    if group:
        chunks = [" ".join(words[i : i + group]) for i in range(0, len(words), group)]
        return ", ".join(chunks)
    return " ".join(words)


def phone_to_words(phone: str) -> str:
    d = "".join(ch for ch in phone if ch.isdigit())
    if len(d) == 11:
        return ", ".join([digits_to_words(d[0]), digits_to_words(d[1:4]), digits_to_words(d[4:7]), digits_to_words(d[7:9]), digits_to_words(d[9:])])
    return digits_to_words(d)


def rate_hint(rate: float) -> str:
    """Map a numeric speed factor to Silero SSML prosody presets (the exact factor is applied by WSOLA)."""
    if rate <= 0.85:
        return "slow"
    if rate < 1.15:
        return "medium"
    if rate < 1.45:
        return "fast"
    return "x-fast"

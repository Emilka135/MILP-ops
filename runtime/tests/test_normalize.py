from datetime import date, time

import pytest

from calls.nlu.normalize import (
    canonicalize,
    extract_digits,
    interpret,
    parse_date,
    parse_daypart,
    parse_time,
    parse_yes_no,
    words_to_number,
)

TODAY = date(2026, 10, 14)  # Wednesday


@pytest.mark.parametrize(
    "text,expected",
    [
        ("четырнадцать", 14),
        ("двадцать один", 21),
        ("сто двадцать один", 121),
        ("две тысячи двадцать шесть", 2026),
        ("девятьсот три", 903),
        ("пятьдесят пять", 55),
        ("ноль", 0),
        ("двадцать первое", 21),
        ("тридцать первого", 31),
        ("третьего", 3),
        ("14", 14),
    ],
)
def test_words_to_number(text, expected):
    assert words_to_number(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("восемь девятьсот три пятьсот пятьдесят пять двенадцать тридцать четыре", "89035551234"),
        ("восемь девять ноль три пять пять пять один два три четыре", "89035551234"),
        ("ноль три", "03"),
        ("сто двадцать три четыреста пятьдесят шесть семьсот восемьдесят девять ноль один", "12345678901"),
        ("8 903 555 12 34", "89035551234"),
        ("8 903 555 00 34", "89035550034"),
        ("десять пять", "105"),
    ],
)
def test_extract_digits(text, expected):
    assert extract_digits(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("да", True),
        ("ага, подходит", True),
        ("ну хорошо, давайте", True),
        ("подтверждаю", True),
        ("нет", False),
        ("не подходит", False),
        ("да нет, не подходит", False),
        ("не очень подходит", False),
        ("нет, отмените", False),
        ("другой день", False),
        ("не знаю", None),
        ("", None),
    ],
)
def test_yes_no(text, expected):
    assert parse_yes_no(text) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("сегодня", date(2026, 10, 14)),
        ("завтра", date(2026, 10, 15)),
        ("послезавтра", date(2026, 10, 16)),
        ("через два дня", date(2026, 10, 16)),
        ("через неделю", date(2026, 10, 21)),
        ("в среду", date(2026, 10, 21)),  # today is Wednesday → next Wednesday
        ("во вторник", date(2026, 10, 20)),
        ("в пятницу", date(2026, 10, 16)),
        ("в следующий понедельник", date(2026, 10, 19)),
        ("в следующую пятницу", date(2026, 10, 23)),
        ("четырнадцатого октября", date(2026, 10, 14)),
        ("14 октября", date(2026, 10, 14)),
        ("14.10", date(2026, 10, 14)),
        ("14.10.2026", date(2026, 10, 14)),
        ("двадцать первого числа", date(2026, 10, 21)),
        ("пятого", date(2026, 11, 5)),
        ("второго ноября", date(2026, 11, 2)),
        ("5 января", date(2027, 1, 5)),
        ("тридцать первое декабря", date(2026, 12, 31)),
        ("запись на 5 ноября 2026 года", date(2026, 11, 5)),
        ("просто поговорить", None),
        ("во второй половине дня", None),
        ("в половине третьего", None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text, TODAY) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("в три часа дня", time(15, 0)),
        ("в три", time(15, 0)),
        ("в 2 часа", time(14, 0)),
        ("в восемь", time(8, 0)),
        ("в 8 утра", time(8, 0)),
        ("в семь вечера", time(19, 0)),
        ("в одиннадцать дня", time(11, 0)),
        ("в двенадцать", time(12, 0)),
        ("в пятнадцать тридцать", time(15, 30)),
        ("15:30", time(15, 30)),
        ("в 10.30", time(10, 30)),
        ("в 14 00", time(14, 0)),
        ("три часа двадцать минут", time(15, 20)),
        ("в половине третьего", time(14, 30)),
        ("в час", time(13, 0)),
        ("через час", None),
        ("до обеда", None),
    ],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("до обеда", "morning"),
        ("утром", "morning"),
        ("с утра", "morning"),
        ("в первой половине дня", "morning"),
        ("после обеда", "afternoon"),
        ("во второй половине дня", "afternoon"),
        ("вечером", "evening"),
        ("после работы", "evening"),
        ("в обед", "midday"),
        ("в три часа дня", None),  # a time, not a day part
        ("через два дня", None),
        ("хоть когда", None),
    ],
)
def test_daypart(text, expected):
    assert interpret(text, TODAY).daypart == expected
    if expected is not None and text not in ("в три часа дня", "через два дня"):
        assert parse_daypart(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("запишите меня на четырнадцатое октября в три часа дня", "запишите меня на 2026-10-14 в 15:00"),
        ("запишите меня на 14 октября в 15:00", "запишите меня на 2026-10-14 в 15:00"),
        ("запишите меня на 14.10 в 15.00", "запишите меня на 2026-10-14 в 15:00"),
        ("во вторник после обеда", "2026-10-20 после обеда"),
        ("мне бы к терапевту на завтра в семь вечера", "мне бы к терапевту на 2026-10-15 в 19:00"),
        ("восемь девятьсот три", "8 903"),
    ],
)
def test_canonicalize_unifies_variants(text, expected):
    assert canonicalize(text, TODAY) == expected


def test_variants_of_14_collapse_to_one_form():
    variants = [
        "четырнадцатого",
        "четырнадцатое число",
        "14-го",
        "14 числа",
        "четырнадцатого октября",
        "14 октября",
        "14.10",
        "14.10.2026",
    ]
    assert {interpret(v, TODAY).date for v in variants} == {date(2026, 10, 14)}


def test_interpretation_fields():
    i = interpret("да, во вторник в 10 утра", TODAY)
    assert i.yes_no is True
    assert i.date == date(2026, 10, 20)
    assert i.weekday_based
    assert i.time == time(10, 0)
    assert i.daypart is None


@pytest.mark.parametrize("text,expected", [
    ("без пятнадцати два", time(13, 45)), ("без четверти три", time(14, 45)), ("без десяти минут восемь утра", time(7, 50)),
    ("без пяти десять", time(9, 55)), ("без двадцати пять вечера", time(16, 40)), ("без двадцати пяти семь", time(18, 35)),
    ("без двадцати три", time(14, 40)), ("без направления к врачу", None), ("без четверти", None),
])
def test_minus_minutes_times(text, expected):
    assert parse_time(text) == expected

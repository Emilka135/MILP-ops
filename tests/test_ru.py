"""Быстрая проверка модулей разбора речи и генерации реплик."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

from app.assistant import ru
from app.assistant.nlu import Nlu

TODAY = date(2026, 9, 30)   # среда, как в примерах регламента

CASES_DATE = [
    "12.03.1978",
    "12.03.78",
    "Смирнова Ольга Викторовна, двенадцатое марта семьдесят восьмого",
    "пятое мая шестьдесят второго",
    "третье января девяностого",
    "двадцатое июля пятьдесят пятого",
    "Смирнова Алиса Дмитриевна, седьмое апреля две тысячи девятнадцатого",
]
CASES_VISIT = [
    ("завтра", date(2026, 10, 1)),
    ("послезавтра", date(2026, 10, 2)),
    ("в четверг", date(2026, 10, 1)),
    ("в пятницу", date(2026, 10, 2)),
    ("на следующей неделе", date(2026, 10, 5)),
    ("через неделю", date(2026, 10, 7)),
]
# Проверяем через разбор: синтез обязан давать текст, который ассистент сам
# понимает — так формат регламента («в четверг, первого октября») и длина
# реплики остаются живой проверкой, а не сверкой строки.
CASES_WORDS = [
    (date(2026, 10, 1), "в четверг, первого октября"),
    (date(2026, 10, 2), "в пятницу, второго октября"),
]
CASES_WORDS_EXTRA = [
    (date(2026, 9, 30), "сегодня, в среду, тридцатого сентября"),
    (date(2026, 10, 1), "завтра, в четверг, первого октября"),
]
CASES_TIME = [
    ("в восемь сорок пять", "08:45"),
    ("в пятнадцать пятнадцать", "15:15"),
    ("в девять ноль-ноль", "09:00"),
    ("10:30", "10:30"),
]
CASES_INTENT = [
    ("Запишите меня к терапевту", "I-01"),
    ("Во сколько принимает хирург?", "I-06"),
    ("Отмените запись к лору", "I-03"),
    ("Хочу перенести запись на другой день", "I-02"),
    ("На когда я записан к окулисту?", "I-04"),
    ("Нужно вызвать врача на дом", "I-05"),
    ("Готовы ли мои анализы?", "I-07"),
    ("Сколько стоит УЗИ щитовидки?", "I-08"),
    ("Хочу пожаловаться на регистратуру", "I-12"),
    ("Как прикрепиться к поликлинике?", "I-10"),
]

failures = 0


def check(label, got, want):
    global failures
    if got != want:
        failures += 1
        print(f"  FAIL {label}: получили {got!r}, ожидали {want!r}")
    else:
        print(f"  ok   {label} → {got!r}")


print("== Даты рождения ==")
for text in CASES_DATE:
    got = ru.parse_birth_date(text)
    print(f"  {text!r} → {got}")
    if got is None:
        failures += 1
        print("  FAIL: не разобрано")

print("\n== Относительные даты (от 30.09.2026) ==")
for text, want in CASES_VISIT:
    check(text, ru.parse_date_ru(text, TODAY), want)

print("\n== Даты словами (регламент 3.2) ==")
for value, want in CASES_WORDS:
    got = ru.date_words(value, TODAY)
    if want not in got:
        failures += 1
        print(f"  FAIL {value}: {got!r} не содержит эталонного {want!r}")
    else:
        print(f"  ok   {value} → {got!r}")
for value, want in CASES_WORDS_EXTRA:
    check(str(value), ru.date_words(value, TODAY), want)

print("\n== Время словами ==")
for text, want in CASES_TIME:
    got = ru.parse_time_ru(text)
    check(text, got.strftime("%H:%M") if got else None, want)

print("\n== Реплика пациента с датой рождения ==")
print("  ", ru.month_date_words("12.03.1978"))

print("\n== Интенты ==")
nlu = Nlu()
for text, want in CASES_INTENT:
    check(text, nlu.classify(text), want)

print("\n== Извлечение специальности ==")
for text, want in [("хочу к ухо-горло-носу", "оториноларинголог"),
                   ("запишите к окулисту", "офтальмолог"),
                   ("по щитовидке надо", "эндокринолог"),
                   ("мне к зубному", "стоматолог")]:
    check(text, nlu.specialty_of(ru.normalize(text)), want)

print(f"\nИТОГО: {'всё прошло' if failures == 0 else str(failures) + ' ошибок'}")
sys.exit(1 if failures else 0)

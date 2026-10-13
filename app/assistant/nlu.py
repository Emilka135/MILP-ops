"""NLU по правилам: интент + параметры (слоты) из реплики пациента.

Осознанный выбор для хакатона: категориальные решения (красные флаги,
«оператор», медицинский вопрос) принимает safety-слой, а не модель;
здесь — только извлечение цели и параметров из русской речи. Работает
оффлайн, без внешних сервисов, полностью предсказуемо.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, Optional

from app.assistant import ru
from app.types import Intent


@dataclass
class NluResult:
    """Результат разбора одной реплики пациента."""

    intent: str = Intent.UNKNOWN.value
    specialty: Optional[str] = None
    doctor_name: Optional[str] = None
    date: Optional[date] = None           # дата визита, о которой просит пациент
    time: Optional[time] = None
    part_of_day: Optional[str] = None
    banned_weekdays: tuple[int, ...] = ()
    full_name: Optional[str] = None
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    birth_date: Optional[date] = None
    yes: bool = False
    no: bool = False
    ordinal: Optional[int] = None       # «вторую», «первую» — выбор слота
    phone: Optional[str] = None
    address: Optional[str] = None
    reason: Optional[str] = None        # повод вызова на дом, дословно
    raw: str = ""
    debug: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Маркеры интентов
# --------------------------------------------------------------------------

_BOOK = (
    "запишите", "записать", "запиши", "записаться", "записатся", "талон", "талончик",
    "номерок", "попасть на прием", "попасть на приём", "хочу на прием", "хочу к",
    "нужен прием", "нужен приём", "можно на прием", "можно на приём", "когда есть",
    "на когда есть", "запишите к", "нужен талон", "хочу попасть", "на прием к",
    "запишите меня",
)
_RESCHEDULE = (
    "перенесите", "перенести", "перенеси", "перезапишите", "перезаписаться",
    "поменять время", "сдвинуть", "на другой день", "другое время", "перенос",
    "перезапишите меня", "поменяйте время", "другой день", "на следующую неделю",
    "не могу прийти", "не смогу прийти", "не получится прийти",
)
_CANCEL = (
    "отмените", "отменить", "отмени", "отмена", "не приду", "снять запись",
    "вычеркните", "не нужно уже", "уже не нужно", "не надо", "отказываюсь от записи",
    "выздоровел", "передумал", "не поеду",
)
_CHECK = (
    "на когда я записан", "когда я записан", "во сколько мне", "во сколько у меня",
    "какой кабинет", "проверить запись", "моя запись", "у меня запись", "на какое время",
    "когда у меня прием", "когда у меня приём", "где моя запись", "напомните мою запись",
    "какая у меня запись", "к какому врачу я записан",
)
_HOME_VISIT = (
    "вызвать врача", "вызвать на дом", "врач на дом", "врача на дом", "на дом врача",
    "домой врача", "нужен врач домой", "вызов на дом", "чтобы пришли", "пришел врач",
    "пришёл врач", "вызовите врача", "врача домой", "педиатра домой",
)
_SCHEDULE = (
    "когда принимает", "во сколько принимает", "график работы", "расписание",
    "до скольки работаете", "до скольки вы работаете", "во сколько открываетесь",
    "работаете в субботу", "работаете в воскресенье", "как вы работаете",
    "часы работы", "режим работы", "принимает ли", "работает ли", "когда работает",
    "график врача", "график приема",
)
_LAB = (
    "анализы", "анализ", "результаты", "результат", "готовы ли", "пришли ли",
    "пришла ли кровь", "кровь готова", "готовность", "пришли результаты",
)
_PAID = (
    "сколько стоит", "стоимость", "цена", "прайс", "платно", "платный", "за деньги",
    "без направления за", "платную", "дмс", "по дмс", "договор",
)
_WAITLIST = (
    "лист ожидания", "в очередь", "запишите в очередь", "позвоните когда появится",
    "нет талонов", "нет свободного времени", "когда появится время", "поставьте в очередь",
)
_ATTACH = (
    "прикрепиться", "прикрепление", "прикрепится", "как прикрепиться", "полис омс",
    "сменить поликлинику", "выписка из медкарты", "справка", "больничный", "рецепт",
    "диспансеризация", "прививка", "флюорография", "что взять с собой", "госуслуги",
    "налоговый вычет", "копия документов", "медкарта",
)
_COMPLAINT_HINTS = ("пожаловаться", "жалоба", "безобразие", "нагрубил", "хамство")


def _contains_any(text: str, markers: tuple[str, ...]) -> Optional[str]:
    for m in markers:
        if m in text:
            return m
    return None


# --------------------------------------------------------------------------
# Имена
# --------------------------------------------------------------------------

_NAME_RE = re.compile(
    r"\b([А-ЯЁ][а-яё]{1,25})\s+([А-ЯЁ][а-яё]{1,25})(?:\s+([А-ЯЁ][а-яё]{1,25}))?\b"
)
_STOP_NAME_WORDS = {
    "здравствуйте", "добрый", "доброе", "пожалуйста", "спасибо", "запишите",
    "скажите", "подскажите", "можно", "хочу", "нужно", "сегодня", "завтра",
    "поликлиника", "регистратура", "виртуальный", "помощник", "разговор",
}


def extract_name(raw: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Найти «Фамилия Имя Отчество» в исходной (не приведённой) реплике."""
    if not raw:
        return None, None, None
    cleaned = raw.replace("ё", "е").replace("Ё", "Е")
    for m in _NAME_RE.finditer(cleaned):
        parts = [p for p in m.groups() if p]
        if parts[0].lower() in _STOP_NAME_WORDS or parts[1].lower() in _STOP_NAME_WORDS:
            continue
        last, first = parts[0], parts[1]
        middle = parts[2] if len(parts) > 2 else None
        if re.fullmatch(r"(Двенадцатое|Третье|Пятое|Седьмое|Восьмое|Первое)", first):
            continue
        return last, first, middle
    return None, None, None


_RE_PHONE = re.compile(r"(?:\+7|8)?[\s\-()]*(\d{3})[\s\-()]*(\d{2})[\s\-()]*(\d{2})[\s\-()]*(\d{2})")


def extract_phone(raw: str) -> Optional[str]:
    m = _RE_PHONE.search(raw)
    if not m:
        return None
    digits = "".join(m.groups())
    return f"+7 ({digits[0:3]}) {digits[3:5]}-{digits[5:7]}-{digits[7:9]}"


_RE_ADDRESS = re.compile(
    r"((?:улица|ул\.?|проспект|пр-т|переулок|шоссе|бульвар)\s+[А-Яа-яЁё0-9\.\- ]+"
    r"(?:,?\s*(?:дом|д\.)\s*\d+[А-Яа-я]?)?(?:,?\s*(?:квартира|кв\.)\s*\d+)?)",
    re.IGNORECASE,
)


def extract_address(raw: str) -> Optional[str]:
    m = _RE_ADDRESS.search(raw)
    return m.group(1).strip() if m else None


_RE_ORDINAL = re.compile(r"\b(перв\w*|втор\w*|треть\w*|четверт\w*|пят\w*)\b")
_ORDINAL_MAP = {"перв": 1, "втор": 2, "треть": 3, "четверт": 4, "пят": 5}


def extract_ordinal(text: str) -> Optional[int]:
    m = _RE_ORDINAL.search(text)
    if m:
        stem = m.group(1)[:5]
        for key, value in _ORDINAL_MAP.items():
            if stem.startswith(key):
                return value
    return None


RE_YES = re.compile(r"\b(да|конечно|верно|правильно|подходит|угу|ага|хорошо|давайте|ок|окей|"
                    r"согласен|согласна|именно так|все верно|точно|подтверждаю)\b")
RE_NO = re.compile(r"\b(нет|не надо|не нужно|не хочу|отказываюсь|неверно|неправильно|"
                   r"не подходит|не буду|никак|нельзя)\b")


def yes_no(text: str) -> tuple[bool, bool]:
    """Разделение «да» и «нет» с учётом «да нет, наверное»."""
    t = ru.normalize(text)
    no = bool(RE_NO.search(t))
    yes = bool(RE_YES.search(t))
    if "да нет" in t or "нет, спасибо" in t:
        return False, True
    if no and yes:
        return False, True
    return yes and not no, no


# --------------------------------------------------------------------------
# Основной разбор
# --------------------------------------------------------------------------


class Nlu:
    """Разбор реплики: интент и параметры. Без побочных эффектов."""

    def __init__(self, specialties: dict[str, str] | None = None,
                 today_provider: Optional[Any] = None) -> None:
        self._specialties = dict(ru._SPECIALTY_INDEX)
        if specialties:
            for name in specialties:
                self._specialties.setdefault(name, name)
        # Часы демо: относительные даты («завтра») обязаны считаться от того же
        # момента, что и слот-движок, иначе «завтра» уедет на реальную дату.
        self._today = today_provider

    @property
    def today(self) -> date:
        if self._today is not None:
            try:
                value = self._today()
                return value.date() if hasattr(value, "date") else value
            except Exception:
                pass
        return date.today()

    # -- специальность ----------------------------------------------------
    def specialty_of(self, text: str) -> Optional[str]:
        t = ru.normalize(text)
        best: tuple[int, Optional[str]] = (0, None)
        for syn, canon in self._specialties.items():
            if not syn:
                continue
            if syn in t:
                # длинное совпадение важнее короткого: «невролог» > «врач»
                if len(syn) > best[0]:
                    best = (len(syn), canon)
        return best[1]

    def doctor_surname(self, text: str) -> Optional[str]:
        m = re.search(r"\b([А-ЯЁ][а-яё]{2,20})(?:ой|ой|у|а|е|ым|ым)?\b", text or "")
        return m.group(1) if m else None

    # -- интент -----------------------------------------------------------
    def classify(self, text: str) -> str:
        t = ru.normalize(text)
        if not t:
            return Intent.UNKNOWN.value
        if _contains_any(t, _COMPLAINT_HINTS):
            return Intent.COMPLAINT.value
        specialty = self.specialty_of(t)
        has_date = ru.parse_date_ru(t, self.today) is not None

        # Вопросы-справки важнее просьбы записаться
        if _contains_any(t, _SCHEDULE) and not _contains_any(t, _RESCHEDULE):
            return Intent.SCHEDULE.value
        if _contains_any(t, _LAB):
            return Intent.LAB_RESULTS.value
        if _contains_any(t, _PAID) and not _contains_any(t, _BOOK):
            return Intent.PAID.value
        if _contains_any(t, _HOME_VISIT):
            return Intent.HOME_VISIT.value
        if _contains_any(t, _WAITLIST):
            return Intent.WAITLIST.value

        if _contains_any(t, _RESCHEDULE):
            return Intent.RESCHEDULE.value
        if _contains_any(t, _CANCEL):
            return Intent.CANCEL.value
        if _contains_any(t, _CHECK) and not _contains_any(t, _BOOK):
            return Intent.CHECK.value
        if _contains_any(t, _BOOK):
            return Intent.BOOK.value
        if specialty:
            return Intent.BOOK.value
        if _contains_any(t, _ATTACH):
            return Intent.ATTACH.value
        if has_date:
            return Intent.UNKNOWN.value
        return Intent.UNKNOWN.value

    # -- параметры --------------------------------------------------------
    def extract(self, raw: str, *, intent: str | None = None) -> NluResult:
        text = raw or ""
        t = ru.normalize(text)
        res = NluResult(raw=text)
        res.intent = intent or self.classify(text)

        res.specialty = self.specialty_of(t)
        res.part_of_day = ru.part_of_day(t)
        res.banned_weekdays = ru.parse_weekday_ban(t)
        res.time = ru.parse_time_ru(t)
        res.ordinal = extract_ordinal(t)
        yes, no = yes_no(text)
        res.yes, res.no = yes, no

        last, first, middle = extract_name(text)
        res.last_name, res.first_name, res.middle_name = last, first, middle
        if last:
            res.full_name = " ".join(p for p in (last, first, middle) if p)

        # «не в пятницу» — это запрет дня, а не желаемая дата: иначе фильтр
        # «только не в пятницу» превращался бы в требование пятницы.
        if res.banned_weekdays:
            banned_names = tuple(ru.WEEKDAYS_NOM[idx] for idx in res.banned_weekdays)
            banned_acc = tuple(ru.WEEKDAYS_ACC[idx] for idx in res.banned_weekdays)
            if any(name in t for name in banned_names + banned_acc):
                res.debug["date_from_banned_weekday"] = True
            if res.date and res.date.weekday() in res.banned_weekdays:
                res.date = None

        birth = ru.parse_birth_date(text)
        if birth:
            res.birth_date = birth
            res.debug["birth_year_known"] = birth.year != self.today.year

        # Дата визита — ВСЕГДА разбираем отдельно: если пациент назвал и дату
        # рождения, и день приёма в одной реплике, терять день приёма нельзя.
        visit_date = ru.parse_date_ru(t, self.today)
        if visit_date and visit_date != birth:
            res.date = visit_date
        if res.date is not None and res.date.weekday() in (res.banned_weekdays or ()):
            res.date = None

        res.phone = extract_phone(text)
        res.address = extract_address(text)
        res.debug["normalized"] = t
        return res


__all__ = ["NluResult", "Nlu", "extract_name", "extract_phone", "extract_address", "yes_no"]

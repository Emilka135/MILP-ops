"""Safety-слой голосового ассистента регистратуры поликлиники № 7.

Класс :class:`SafetyLayer` реализует контракт ARCHITECTURE.md 3.3:

    layer = SafetyLayer()
    match = layer.check("ой, у меня сильно давит в груди")
    match.verdict            # SafetyVerdict.RED_FLAG_A
    match.rule               # "R-01"
    match.category           # "Сердце, грудь"
    layer.boundary_phrase("лекарства")   # граничная фраза для медицинских вопросов

Принципы (регламент РГ-ТЕЛ-01, разделы 7.1, 7.4):

* красный флаг категории А прерывает любой сценарий в любой реплике;
* при сомнении флаг считается сработавшим: ложная тревога допустима, пропуск — нет;
* слова «оператор / человек / сотрудник / регистратор / живой / соедини» —
  немедленный перевод, без уговоров (R-02);
* медицинские вопросы — только граничная фраза: без препаратов, дозировок,
  диагнозов и значений анализов (раздел 3.8);
* ``check()`` не выбрасывает исключений ни при каком вводе.

Распознавание идёт по смыслу: нормализация регистра и «ё/е», исправление
типичных опечаток распознавания речи, сопоставление по основам словоформ
(префиксное сравнение), учёт отрицания («не болит» — не флаг, «нет боли в
груди» — не флаг) и прошедшего времени чужого опыта.

Прошедшее время чужого опыта («у мамы год назад была боль в груди») —
*мягкое* срабатывание: вердикт остаётся :attr:`SafetyVerdict.RED_FLAG_A`
(пропускать нельзя), но в ``reason`` добавляется маркер ``soft=True``, а в
``context["safety"]["soft"]`` — ``True``. Вызывающий сначала произносит фразу
про 103/112, затем продолжает исходный сценарий (С-14, ветвление «ложное
срабатывание»).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

try:  # обычный запуск из корня репозитория
    from app.types import SafetyMatch, SafetyVerdict
except ImportError:  # pragma: no cover — запуск файла напрямую
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.types import SafetyMatch, SafetyVerdict


# --------------------------------------------------------------------------
# Пути и константы
# --------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_GLOSSARY_PATH = "app/safety/red_flags.json"

#: Реплики-подсказки для движка диалога (все — не длиннее 20 слов).
REPLY_RED_FLAG_A = (
    "Немедленно звоните сто три или сто двенадцать. Повторяю: сто три или сто двенадцать. "
    "Не ждите приёма в поликлинике."
)
REPLY_RED_FLAG_A_SOFT = (
    "Звоните сто три или сто двенадцать. Если сейчас всё в порядке, помогу с вашим вопросом."
)
REPLY_RED_FLAG_B = (
    "Соединяю с регистратором, он организует срочную помощь. Если станет хуже — сразу звоните сто три."
)
REPLY_OPERATOR = "Соединяю с сотрудником."
REPLY_COMPLAINT = "Мне жаль, что так вышло. Соединяю вас со старшим администратором."
REPLY_SPD = "Этот запрос оформляется письменно. Соединяю с администратором."
REPLY_RECORD_OPT_OUT = "Хорошо, запись разговора отключена. Продолжаем."
REPLY_SILENCE = "Вас не слышно. Перезвоните, пожалуйста."
REPLY_THIRD_PARTY = (
    "Сведения о записях могу сообщить только самому пациенту. "
    "Могу отправить сообщение на его номер."
)

#: Маршрут (раздел 7.2/7.3) и приоритет очереди (раздел 7.5) по вердикту.
ROUTES: dict[SafetyVerdict, tuple[str, str]] = {
    SafetyVerdict.RED_FLAG_A: ("М0 + М2", "P1"),
    SafetyVerdict.RED_FLAG_B: ("М2 → М4-Н", "P1"),
    SafetyVerdict.OPERATOR: ("М2", "P2"),
    SafetyVerdict.COMPLAINT: ("М3", "P2"),
    SafetyVerdict.MEDICAL: ("М2", "P3"),
    SafetyVerdict.SPD_CONSENT: ("М3", "P3"),
    SafetyVerdict.THIRD_PARTY: ("М2", "P3"),
    SafetyVerdict.RECORD_OPT_OUT: ("", ""),
    SafetyVerdict.SILENCE: ("Завершение", ""),
    SafetyVerdict.OK: ("", ""),
}

BOUNDARY_PHRASES: dict[str, str] = {
    "default": "Я не могу давать медицинские советы — это может сделать только врач. Записать вас к терапевту?",
    "drugs": "Я не могу советовать лекарства — это решит врач на приёме. Записать вас к терапевту?",
    "results": "Я не вижу значений результатов и не могу их оценивать. Разъяснит лечащий врач. Записать вас?",
    "diagnosis": "Оценить состояние может только врач. Давайте я запишу вас на приём.",
    "which_doctor": "Начните с участкового терапевта — он направит к нужному специалисту. Записать вас?",
    "worsening": "Если состояние резко ухудшилось, звоните сто три. Записать вас к терапевту?",
}

_BOUNDARY_TOPIC_KEYS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("лекарств", "препарат", "таблет", "выпить", "принять", "дозиров", "пить"), "drugs"),
    (("анализ", "результат", "холестерин", "сахар", "гемоглобин", "показател", "значени"), "results"),
    (("диагноз", "что это", "опасно", "серьезно", "нормально ли"), "diagnosis"),
    (("к какому врачу", "какой врач", "специалист", "к какому специалисту"), "which_doctor"),
    (("ухудш", "срочно", "103", "сто три", "экстренн"), "worsening"),
)


# --------------------------------------------------------------------------
# Минимальный встроенный словарь: страховка, если red_flags.json недоступен
# или повреждён. Красные флаги категории А не должны пропадать никогда.
# --------------------------------------------------------------------------

_FALLBACK_GLOSSARY: dict[str, Any] = {
    "version": "fallback",
    "source": "встроенная страховка (РГ-ТЕЛ-01, 7.4)",
    "rules": {
        "a": "R-01", "b": "R-11", "operator": "R-02", "complaint": "R-03",
        "medical": "R-04", "third_party": "R-07", "spd_consent": "R-16",
        "record_opt_out": "", "silence": "R-17",
    },
    "category_a": [
        {"group": "Сердце, грудь", "phrases": [
            "боль в груди", "боли в груди", "болит в груди", "болит грудь", "давит в груди",
            "давит в грудь", "давит за грудиной", "сжимает в груди", "жжет в груди",
            "боль в сердце", "сердце болит", "прихватило сердце", "отдает в левую руку",
            "сердечный приступ"], "words": ["инфаркт", "стенокард"]},
        {"group": "Дыхание", "phrases": [
            "не может дышать", "не могу дышать", "не может вдохнуть", "не хватает воздуха",
            "трудно дышать", "тяжело дышать", "не дышит"], "words": ["задых", "задохн", "удуш", "посине", "хрип", "подавил"]},
        {"group": "Сознание", "phrases": [
            "потерял сознание", "потеряла сознание", "без сознания", "не приходит в себя",
            "не могу разбудить", "не просыпается", "упал в обморок"], "words": ["обморок", "бессознательн", "отключк"]},
        {"group": "Инсульт", "phrases": [
            "перекосило лицо", "отнялась рука", "отнялась нога", "не может говорить",
            "речь невнятная", "онемела половина тела"], "words": ["инсульт", "парализ", "онемел", "перекос*", "отнял"]},
        {"group": "Судороги", "phrases": [
            "судороги", "припадок", "конвульсии", "трясет не останавливается"], "words": ["судорог", "припадок", "конвульс", "эпилепс"]},
        {"group": "Кровотечение", "phrases": [
            "сильное кровотечение", "кровотечение", "кровь не останавливается", "рвота кровью",
            "кровь изо рта", "кровь идет"], "words": ["кровотеч", "кровохарк"]},
        {"group": "Травмы, отравления", "phrases": [
            "дтп", "сильно ударился головой", "упал с высоты", "ожог большой", "сильный ожог",
            "отравился", "выпил химию", "проглотил таблетки", "наглотался таблеток",
            "укусила змея"], "words": ["отравил", "отравлен", "дтп", "передоз"]},
        {"group": "Аллергия", "phrases": [
            "отекает горло", "отек горла", "не может глотать", "анафилактический шок",
            "отекает лицо", "отек квинке"], "words": ["анафилакс"]},
        {"group": "Дети", "phrases": [
            "младенец вялый", "ребенок вялый", "младенец не просыпается", "ребенок не просыпается",
            "у грудного ребенка температура", "ребенок посинел", "ребенок проглотил"], "words": []},
        {"group": "Беременность", "phrases": [
            "беременная кровотечение", "кровотечение при беременности", "отошли воды",
            "начались схватки", "сильная боль в животе при беременности"], "words": ["схватк"]},
        {"group": "Угроза себе или другим", "phrases": [
            "не хочу жить", "хочу покончить с собой", "покончить с собой", "хочу умереть",
            "наглотался таблеток", "угрожает убить", "порежу вены"], "words": ["суицид", "самоубийств"]},
    ],
    "category_b": [
        {"group": "Острая боль", "phrases": ["очень сильная боль", "невыносимая боль", "невыносимо болит", "сильная боль", "боль усиливается"], "words": []},
        {"group": "Высокая температура", "phrases": ["температура сорок", "температура 40", "температура 39", "высокая температура", "температура не сбивается"], "words": []},
        {"group": "Резкое ухудшение", "phrases": ["стало намного хуже", "резко стало хуже", "резко поднялось давление", "не могу встать", "очень плохо стало", "сильная слабость"], "words": []},
        {"group": "Травма без угрозы жизни", "phrases": ["подвернул ногу", "распухла нога", "глубоко порезался", "глубокая рана", "перелом", "вывих"], "words": ["перелом", "вывих"]},
    ],
    "operator": {
        "words": ["оператор", "сотрудник", "регистратор", "соедини", "переключ"],
        "exact_words": ["живой", "живого", "живому", "живым", "живая", "живые"],
        "phrases": ["дайте человека", "дайте трубку", "позовите человека", "нужен человек", "живой человек"],
        "weak_words": ["человек", "человека", "человеку", "человеком", "люди", "людей", "девушка", "девушку", "позовите"],
        "request_words": ["дайте", "дай", "можно", "хочу", "нужен", "нужна", "нужно", "соедини", "позов", "переключ", "трубку"],
    },
    "complaint": {
        "words": ["пожалова", "жалоб", "нагруби", "нахами", "хамств", "безобрази", "возмутительн", "возмуща", "отвратительн", "недовольн"],
        "phrases": ["хочу пожаловаться", "буду жаловаться", "это безобразие", "врач нагрубил"],
    },
    "medical": {
        "phrases": ["что выпить", "что принять", "какие таблетки", "какое лекарство", "чем лечить", "нормальный ли", "нормальные ли", "расшифруйте", "продиктуйте результат", "какой у меня", "холестерин", "это опасно", "к какому врачу"],
        "symptom_words": ["болит", "температура", "кашель", "тошнит", "сыпь", "кружится голова", "одышка", "слабость", "давление"],
        "question_words": ["что", "почему", "как", "можно", "стоит", "надо", "опасно", "чем", "подскажите", "посоветуйте"],
        "advice_words": ["что делать", "посоветуйте", "подскажите", "как быть"],
    },
    "spd_consent": {
        "phrases": ["отзыв согласия", "отзываю согласие", "удалите мои данные", "удалить персональные данные", "копию записи разговора", "удалите запись разговора"],
        "words": ["персональн"],
    },
    "record_opt_out": {
        "phrases": ["не записывайте разговор", "я против записи", "против записи разговора", "отключите запись", "выключите запись", "без записи разговора", "не надо записывать разговор"],
        "words": ["не записывайте"],
        "exclude_if_contains": ["к врачу", "на прием", "на приём", "талон", "меня к"],
    },
    "third_party": {
        "phrases": ["о другом человеке", "его запись", "ее запись", "запись мамы", "мама записана", "она записана", "он записан", "его анализы", "ее анализы", "посторонний человек"],
        "words": ["посторонн"],
        "record_question_words": ["на когда", "когда", "во сколько", "какой кабинет", "готовы", "пришли", "результат"],
        "record_state_words": ["записан", "запись", "талон", "кабинет", "анализ", "результат"],
        "person_words": ["мама", "мамы", "папа", "муж", "жена", "жены", "сын", "сына", "дочь", "дочери", "брат", "сестра", "бабушка", "дедушка", "соседка", "подруга", "знакомый", "коллега"],
        "name_patterns": [],
    },
    "silence": {
        "markers": ["", "тишина", "молчание", "помехи", "шум", "тихо", "нет ответа", "silence", "noise"],
        "noise_pattern": "^[\\s.,;:!?\\-–—_*…()\\[\\]{}\"'«»]*$",
    },
    "negations": ["не", "нет", "ни", "без"],
    "negation_prefixes": ["отсутств"],
    "clause_words": ["но", "а", "однако", "зато", "хотя", "если", "или", "затем", "потом", "когда", "пока", "кроме"],
    "forward_negations": ["нет"],
    "forward_stop_words": ["прошл", "утихл", "отпустил", "перестал"],
    "past_markers": ["год назад", "лет назад", "месяц назад", "неделю назад", "давно", "раньше", "в прошлом", "прошлом году", "в детстве", "уже прошло", "переболел", "перенес"],
    "past_verbs": ["была", "было", "были", "был", "болело", "болела", "болел", "случилось", "произошло"],
    "present_markers": ["сейчас", "прямо сейчас", "только что", "до сих пор", "все еще", "не проходит"],
    "third_party_context": ["у мамы", "у папы", "у мужа", "у жены", "у сына", "у дочери", "у бабушки", "у дедушки", "у него", "у нее", "у них"],
    "typos": {},
}


# --------------------------------------------------------------------------
# Нормализация и построение шаблонов
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[а-яa-z0-9]+")
_BOUNDARY_RE = re.compile(r"[.,;:!?…]|—|–")


def _deep_merge(base: Any, override: Any) -> Any:
    """Рекурсивно наложить ``override`` на ``base`` (dict — по ключам, list — целиком)."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            if key in out:
                out[key] = _deep_merge(out[key], value)
            else:
                out[key] = value
        return out
    if isinstance(base, list) and isinstance(override, list):
        return list(override) if override else list(base)
    if override is None:
        return base
    return override


def _prefix_pattern(word: str) -> str:
    """Шаблон одного слова: короткие слова — точно, длинные — по основе.

    Суффикс ``*`` в словаре означает «словоформа от полного слова»: основа не
    урезается (например ``регистратор*`` ловит «регистратора/регистратору»,
    но не «регистратуру» — это не человек, а окно регистратуры).
    """
    word = word.replace("ё", "е")
    if word.endswith("*") and len(word) > 1:
        return re.escape(word[:-1]) + r"\w*"
    if len(word) <= 3:
        return re.escape(word)
    stem = word[: max(4, len(word) - 2)]
    return re.escape(stem) + r"\w*"


def _compile_phrase(phrase: str, gap: int = 2) -> Optional[re.Pattern[str]]:
    parts = [part for part in phrase.replace("ё", "е").split() if part]
    if not parts:
        return None
    if len(parts) == 1:
        body = _prefix_pattern(parts[0])
    else:
        joiner = r"\s+(?:\w+\s+){0,%d}" % gap
        body = joiner.join(_prefix_pattern(part) for part in parts)
    try:
        return re.compile(r"(?<!\w)" + body + r"(?!\w)")
    except re.error:  # pragma: no cover — защита от правок словаря
        return None


def _levenshtein1(a: str, b: str) -> bool:
    """Расстояние Дамерау-Левенштейна ≤ 1 (без полной матрицы)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = sum(1 for x, y in zip(a, b) if x != y)
        if diff <= 1:
            return True
        if diff == 2:  # перестановка соседних букв
            idx = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
            return len(idx) == 2 and idx[1] == idx[0] + 1 and a[idx[0]] == b[idx[1]] and a[idx[1]] == b[idx[0]]
        return False
    short, long = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] != long[j]:
            if skipped:
                return False
            skipped = True
            j += 1
            continue
        i += 1
        j += 1
    return True


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float)) and str(item).strip()]


class _Group:
    """Одна смысловая группа словаря с готовыми шаблонами."""

    __slots__ = ("name", "patterns", "word_patterns", "note")

    def __init__(self, name: str, phrases: Sequence[str], words: Sequence[str],
                 exact_words: Sequence[str] = (), note: str = "") -> None:
        self.name = name
        self.note = str(note or "")
        self.patterns: list[tuple[re.Pattern[str], str, bool]] = []
        for phrase in phrases:
            regex = _compile_phrase(str(phrase))
            if regex is None:
                continue
            norm = str(phrase).replace("ё", "е").lower()
            has_neg = any(part in ("не", "нет", "ни", "без") for part in norm.split())
            self.patterns.append((regex, str(phrase), has_neg))
        for word in words:
            regex = _compile_phrase(str(word))
            if regex is not None:
                self.patterns.append((regex, str(word), str(word).lower() in ("не", "нет", "без")))
        for word in exact_words:
            try:
                regex = re.compile(r"(?<!\w)" + re.escape(str(word).replace("ё", "е").lower()) + r"(?!\w)")
            except re.error:  # pragma: no cover
                continue
            self.patterns.append((regex, str(word), False))

    @property
    def is_empty(self) -> bool:
        return not self.patterns


# --------------------------------------------------------------------------
# Основной класс
# --------------------------------------------------------------------------


class SafetyLayer:
    """Правила безопасности по каждой реплике пациента.

    Контракт — ARCHITECTURE.md 3.3. Метод :meth:`check` безопасен к любому
    вводу: ``None``, числа, мусор, пустая строка, гигантский текст.
    """

    def __init__(self, *, glossary_path: str = DEFAULT_GLOSSARY_PATH) -> None:
        self.glossary_path = glossary_path or DEFAULT_GLOSSARY_PATH
        self._data, self._loaded_from = _load_glossary(self.glossary_path)
        self._build()

    # -- построение словаря ------------------------------------------------

    def _build(self) -> None:
        data = self._data if isinstance(self._data, dict) else {}
        rules = data.get("rules") if isinstance(data.get("rules"), dict) else {}
        self._rules = {
            "a": str(rules.get("a") or "R-01"),
            "b": str(rules.get("b") or "R-11"),
            "operator": str(rules.get("operator") or "R-02"),
            "complaint": str(rules.get("complaint") or "R-03"),
            "medical": str(rules.get("medical") or "R-04"),
            "third_party": str(rules.get("third_party") or "R-07"),
            "spd_consent": str(rules.get("spd_consent") or "R-16"),
            "record_opt_out": str(rules.get("record_opt_out") or ""),
            "silence": str(rules.get("silence") or "R-17"),
        }

        self._cat_a = [
            _Group(str(item.get("group") or "Категория А"),
                   _as_str_list(item.get("phrases")),
                   _as_str_list(item.get("words")),
                   _as_str_list(item.get("exact_words")),
                   str(item.get("note") or ""))
            for item in _dict_list(data.get("category_a"))
        ]
        self._cat_b = [
            _Group(str(item.get("group") or "Категория Б"),
                   _as_str_list(item.get("phrases")),
                   _as_str_list(item.get("words")),
                   _as_str_list(item.get("exact_words")),
                   str(item.get("note") or ""))
            for item in _dict_list(data.get("category_b"))
        ]

        operator = data.get("operator") if isinstance(data.get("operator"), dict) else {}
        self._operator = _Group("Просьба соединить", _as_str_list(operator.get("phrases")),
                                _as_str_list(operator.get("words")),
                                _as_str_list(operator.get("exact_words")))
        self._operator_weak = _Group("Просьба соединить", (), _as_str_list(operator.get("weak_words")))
        self._operator_request = _Group("Просьба соединить", (), _as_str_list(operator.get("request_words")))

        complaint = data.get("complaint") if isinstance(data.get("complaint"), dict) else {}
        self._complaint = _Group("Жалоба", _as_str_list(complaint.get("phrases")),
                                 _as_str_list(complaint.get("words")))

        medical = data.get("medical") if isinstance(data.get("medical"), dict) else {}
        self._medical = _Group("Медицинский вопрос", _as_str_list(medical.get("phrases")), ())
        self._medical_symptoms = _Group("Медицинский вопрос", (), _as_str_list(medical.get("symptom_words")))
        self._medical_questions = _Group("Медицинский вопрос", (), _as_str_list(medical.get("question_words")))
        self._medical_advice = _Group("Медицинский вопрос", (), _as_str_list(medical.get("advice_words")))

        spd = data.get("spd_consent") if isinstance(data.get("spd_consent"), dict) else {}
        self._spd = _Group("Отзыв согласия на обработку ПДн", _as_str_list(spd.get("phrases")),
                           _as_str_list(spd.get("words")))

        opt_out = data.get("record_opt_out") if isinstance(data.get("record_opt_out"), dict) else {}
        self._opt_out = _Group("Отказ от записи разговора", _as_str_list(opt_out.get("phrases")),
                               _as_str_list(opt_out.get("words")))
        self._opt_out_exclude = _as_str_list(opt_out.get("exclude_if_contains"))

        third = data.get("third_party") if isinstance(data.get("third_party"), dict) else {}
        self._third = _Group("Запрос о другом человеке", _as_str_list(third.get("phrases")),
                             _as_str_list(third.get("words")))
        self._third_questions = _Group("Запрос о другом человеке", (), _as_str_list(third.get("record_question_words")))
        self._third_states = _Group("Запрос о другом человеке", (), _as_str_list(third.get("record_state_words")))
        self._third_persons = _Group("Запрос о другом человеке", (), _as_str_list(third.get("person_words")))
        self._third_name_res: list[re.Pattern[str]] = []
        for pattern in _as_str_list(third.get("name_patterns")):
            try:
                self._third_name_res.append(re.compile(pattern))
            except re.error:  # pragma: no cover
                continue

        silence = data.get("silence") if isinstance(data.get("silence"), dict) else {}
        self._silence_markers = {marker.replace("ё", "е").strip() for marker in _as_str_list(silence.get("markers"))}
        self._silence_markers.add("")
        try:
            self._noise_re = re.compile(str(silence.get("noise_pattern") or r"^\s*$"))
        except re.error:  # pragma: no cover
            self._noise_re = re.compile(r"^\s*$")

        self._negations = tuple(word.lower() for word in _as_str_list(data.get("negations"))) or ("не", "нет", "ни", "без")
        self._negation_prefixes = tuple(word.lower() for word in _as_str_list(data.get("negation_prefixes"))) or ("отсутств",)
        self._clause_words = {word.lower() for word in _as_str_list(data.get("clause_words"))} or {"но", "а", "однако"}
        self._forward_negations = set(word.lower() for word in _as_str_list(data.get("forward_negations"))) or {"нет"}
        self._forward_stops = tuple(word.lower() for word in _as_str_list(data.get("forward_stop_words"))) or ("прошл",)

        self._past_markers = tuple(marker.lower() for marker in _as_str_list(data.get("past_markers")))
        self._past_verbs = tuple(word.lower() for word in _as_str_list(data.get("past_verbs")))
        self._present_markers = tuple(marker.lower() for marker in _as_str_list(data.get("present_markers")))
        self._third_context = tuple(marker.lower() for marker in _as_str_list(data.get("third_party_context")))

        typos = data.get("typos") if isinstance(data.get("typos"), dict) else {}
        self._typos: dict[str, str] = {}
        for bad, good in typos.items():
            bad_norm = str(bad).replace("ё", "е").lower().strip()
            good_norm = str(good).replace("ё", "е").lower().strip()
            if bad_norm and good_norm and " " not in bad_norm and bad_norm != good_norm:
                self._typos[bad_norm] = good_norm

        self._vocab = self._build_vocab()

    def _build_vocab(self) -> dict[str, list[str]]:
        """Словарь для исправления опечаток: первая буква -> слова длиной >= 5."""
        vocab: dict[str, list[str]] = {}
        groups: list[_Group] = [
            *self._cat_a, *self._cat_b, self._operator, self._operator_weak,
            self._operator_request, self._complaint, self._medical,
            self._medical_symptoms, self._medical_questions, self._medical_advice,
            self._spd, self._opt_out, self._third, self._third_questions,
            self._third_states, self._third_persons,
        ]
        seen: set[str] = set()
        for group in groups:
            for _regex, source, _neg in group.patterns:
                for part in str(source).replace("ё", "е").lower().split():
                    part = "".join(ch for ch in part if ch.isalnum())
                    if len(part) >= 5 and part not in seen:
                        seen.add(part)
                        vocab.setdefault(part[0], []).append(part)
        return vocab

    # -- нормализация ------------------------------------------------------

    def _fix_typo(self, word: str) -> str:
        """Исправить типичную опечатку распознавания речи.

        Нечёткое исправление применяется осторожно: только к длинным словам
        (>= 6 букв) с теми же тремя первыми буквами. Иначе «стало» превращалось
        бы в «сжало», «выпил» — в «выпал», а «проходит» — в «приходит», и флаг
        терялся.
        """
        mapped = self._typos.get(word)
        if mapped:
            return mapped
        if len(word) < 6:
            return word
        candidates = self._vocab.get(word[0])
        if not candidates:
            return word
        best: str = ""
        best_delta = 99
        for candidate in candidates:
            if len(candidate) < 6 or candidate[:3] != word[:3]:
                continue
            delta = abs(len(candidate) - len(word))
            if delta > 1 or delta >= best_delta:
                continue
            if _levenshtein1(word, candidate):
                best = candidate
                best_delta = delta
        return best or word

    def _prepare(self, text: Any) -> tuple[str, str, list[str], list[bool]]:
        if isinstance(text, str):
            raw = text
        elif text is None:
            raw = ""
        else:
            try:
                raw = str(text)
            except Exception:  # pragma: no cover — объект с битым __str__
                raw = ""
        low = raw.lower().replace("ё", "е")
        tokens: list[str] = []
        bounds: list[bool] = []
        prev_end = 0
        for found in _WORD_RE.finditer(low):
            between = low[prev_end:found.start()]
            boundary = bool(_BOUNDARY_RE.search(between))
            word = self._fix_typo(found.group(0))
            if word in self._clause_words:
                boundary = True
            tokens.append(word)
            bounds.append(boundary)
            prev_end = found.end()
        return raw, low, tokens, bounds

    # -- сопоставление -----------------------------------------------------

    @staticmethod
    def _word_index(match_str: str, position: int) -> int:
        return match_str.count(" ", 0, position)

    def _is_negated(self, tokens: Sequence[str], bounds: Sequence[bool],
                    start: int, end: int, has_neg_in_phrase: bool) -> bool:
        if has_neg_in_phrase:
            return False
        # отрицание перед фразой («не болит», «нет боли в груди»)
        index = start - 1
        steps = 0
        while index >= 0 and steps < 3:
            if index + 1 < len(bounds) and bounds[index + 1]:
                return False
            word = tokens[index]
            if word in self._clause_words:
                return False
            if word in self._negations or any(word.startswith(prefix) for prefix in self._negation_prefixes):
                return True
            index -= 1
            steps += 1
        # отрицание после фразы («боли нет», «боль прошла»)
        for forward in range(end + 1, min(end + 3, len(tokens))):
            if bounds[forward]:
                break
            word = tokens[forward]
            if word in self._forward_negations:
                return True
            if any(word.startswith(stop) for stop in self._forward_stops):
                return True
        return False

    def _find_in_group(self, group: _Group, match_str: str, tokens: Sequence[str],
                       bounds: Sequence[bool]) -> Optional[dict[str, Any]]:
        for regex, source, has_neg in group.patterns:
            for found in regex.finditer(match_str):
                start = self._word_index(match_str, found.start())
                end = self._word_index(match_str, found.end())
                if self._is_negated(tokens, bounds, start, end, has_neg):
                    continue
                return {"matched": found.group(0), "source": source, "start": start, "end": end}
        return None

    def _soft_info(self, low: str, tokens: Sequence[str]) -> tuple[bool, str]:
        past_time = next((marker for marker in self._past_markers if marker and marker in low), "")
        past_verb = next((word for word in tokens if any(word.startswith(v) for v in self._past_verbs)), "")
        third = next((marker for marker in self._third_context if marker and marker in low), "")
        present = next((marker for marker in self._present_markers if marker and marker in low), "")
        parts: list[str] = []
        if past_time:
            parts.append(f"время: «{past_time}»")
        if past_verb:
            parts.append(f"прошедшее время: «{past_verb}»")
        if third:
            parts.append(f"чужой опыт: «{third}»")
        if present:
            parts.append(f"есть признак «сейчас»: «{present}»")
        soft = bool(past_time and (third or past_verb) and not present)
        return soft, "; ".join(parts)

    def _silence_match(self, raw: str, tokens: Sequence[str]) -> Optional[SafetyMatch]:
        collapsed = " ".join(tokens)
        if not tokens:
            return self._verdict(SafetyVerdict.SILENCE, "Тишина, помехи", self._rules["silence"],
                                 "пустая реплика или помехи — R-17", matched=raw.strip()[:60])
        if collapsed in self._silence_markers:
            return self._verdict(SafetyVerdict.SILENCE, "Тишина, помехи", self._rules["silence"],
                                 f"маркер тишины: «{collapsed}» — R-17", matched=collapsed)
        if self._noise_re.match(raw):
            return self._verdict(SafetyVerdict.SILENCE, "Тишина, помехи", self._rules["silence"],
                                 "реплика из одних знаков — тишина, R-17", matched=raw.strip()[:60])
        return None

    def _verdict(self, verdict: SafetyVerdict, category: str, rule: str,
                 reason: str, *, matched: str = "") -> SafetyMatch:
        return SafetyMatch(verdict=verdict, rule=rule, category=category,
                           matched=matched[:160], reason=reason[:400])

    # -- публичный контракт ------------------------------------------------

    def check(self, text: str, *, context: dict | None = None) -> SafetyMatch:
        """Решение safety-слоя по одной реплике пациента. Не выбрасывает исключений."""
        try:
            match = self._check(text, context)
        except Exception as exc:  # pragma: no cover — страховка от любых сбоев
            match = self._verdict(
                SafetyVerdict.OK, "Ошибка разбора", "",
                f"внутренняя ошибка safety-слоя: {exc.__class__.__name__}; реплика обработана без срабатывания",
            )
        try:
            if isinstance(context, dict):
                context["safety"] = {
                    "verdict": match.verdict.value,
                    "rule": match.rule,
                    "category": match.category,
                    "matched": match.matched,
                    "reason": match.reason,
                    "soft": self.is_soft(match),
                    "route": self.route(match),
                    "priority": self.priority(match),
                }
        except Exception:  # pragma: no cover
            pass
        return match

    def _check(self, text: Any, context: dict | None = None) -> SafetyMatch:
        raw, low, tokens, bounds = self._prepare(text)
        match_str = " ".join(tokens)

        silence = self._silence_match(raw, tokens)
        if silence is not None:
            return silence

        # 1. Категория А — возможная угроза жизни. Проверяется всегда.
        for group in self._cat_a:
            hit = self._find_in_group(group, match_str, tokens, bounds)
            if hit:
                soft, extra = self._soft_info(low, tokens)
                detail = group.note or "угроза жизни"
                if extra:
                    detail = f"{detail}; {extra}"
                if soft:
                    reason = (f"soft=True; категория А ({group.name}), «{hit['matched']}»; {detail}. "
                              f"R-01: сначала фраза про 103/112, затем продолжить сценарий.")
                else:
                    reason = (f"категория А ({group.name}), «{hit['matched']}»; {detail}. "
                              f"R-01: 103/112, перевод P1, сценарий прерывается.")
                return self._verdict(SafetyVerdict.RED_FLAG_A, group.name, self._rules["a"],
                                     reason, matched=hit["matched"])

        # 2. Категория Б — острое состояние без явной угрозы жизни.
        for group in self._cat_b:
            hit = self._find_in_group(group, match_str, tokens, bounds)
            if hit:
                soft, extra = self._soft_info(low, tokens)
                detail = group.note or "острое состояние"
                if extra:
                    detail = f"{detail}; {extra}"
                if soft:
                    reason = (f"soft=True; категория Б ({group.name}), «{hit['matched']}»; {detail}. "
                              f"R-11: фраза про регистратора, затем продолжить сценарий.")
                else:
                    reason = (f"категория Б ({group.name}), «{hit['matched']}»; {detail}. "
                              f"R-11: регистратор, приоритет P1.")
                return self._verdict(SafetyVerdict.RED_FLAG_B, group.name, self._rules["b"],
                                     reason, matched=hit["matched"])

        # 3. Просьба соединить с человеком — всегда немедленный перевод.
        hit = self._find_in_group(self._operator, match_str, tokens, bounds)
        if hit is None:
            weak = self._find_raw(self._operator_weak, match_str)
            if weak and (self._find_raw(self._operator_request, match_str) or len(tokens) <= 2):
                hit = weak
        if hit:
            return self._verdict(SafetyVerdict.OPERATOR, "Просьба соединить с человеком",
                                 self._rules["operator"],
                                 f"просьба соединить с человеком: «{hit['matched']}»; R-02, перевод без уговоров",
                                 matched=hit["matched"])

        # 4. Жалоба, недовольство, агрессия (R-03).
        hit = self._find_in_group(self._complaint, match_str, tokens, bounds)
        if hit:
            return self._verdict(SafetyVerdict.COMPLAINT, "Жалоба, конфликт", self._rules["complaint"],
                                 f"жалоба или конфликт: «{hit['matched']}»; R-03, старший администратор, P2",
                                 matched=hit["matched"])

        # 5. Отзыв согласия на обработку ПДн (R-16).
        hit = self._find_in_group(self._spd, match_str, tokens, bounds)
        if hit:
            return self._verdict(SafetyVerdict.SPD_CONSENT, "Отзыв согласия на обработку ПДн",
                                 self._rules["spd_consent"],
                                 f"запрос по персональным данным: «{hit['matched']}»; R-16, письменно, старший администратор",
                                 matched=hit["matched"])

        # 6. Отказ от записи разговора (раздел 3.4).
        hit = self._find_in_group(self._opt_out, match_str, tokens, bounds)
        if hit and not any(bad in low for bad in self._opt_out_exclude):
            return self._verdict(SafetyVerdict.RECORD_OPT_OUT, "Отказ от записи разговора",
                                 self._rules["record_opt_out"],
                                 f"пациент против записи разговора: «{hit['matched']}»; запись отключается, обслуживание продолжается",
                                 matched=hit["matched"])

        # 7. Запрос сведений о другом человеке (правило «пациент называет первым»).
        third = self._find_in_group(self._third, match_str, tokens, bounds)
        if third is None and self._is_third_party_request(match_str, low, tokens):
            third = {"matched": match_str[:80], "source": "смысловая конструкция"}
        if third:
            return self._verdict(SafetyVerdict.THIRD_PARTY, "Запрос о другом человеке",
                                 self._rules["third_party"],
                                 f"запрос сведений о другом человеке: «{third['matched']}»; сведения только пациенту (раздел 3.5)",
                                 matched=third["matched"])

        # 8. Медицинский вопрос — только граничная фраза (раздел 3.8).
        hit = self._find_in_group(self._medical, match_str, tokens, bounds)
        if hit is None and self._is_medical_question(match_str):
            hit = {"matched": match_str[:80], "source": "смысловая конструкция"}
        if hit is None and self._context_medical(tokens, context):
            hit = {"matched": match_str[:80], "source": "контекст сценария С-08"}
        if hit:
            return self._verdict(SafetyVerdict.MEDICAL, "Медицинский вопрос", self._rules["medical"],
                                 f"медицинский вопрос: «{hit['matched']}»; R-04, граничная фраза без препаратов и значений",
                                 matched=hit["matched"])

        return self._verdict(SafetyVerdict.OK, "", "", "")

    def _find_raw(self, group: _Group, match_str: str) -> Optional[dict[str, Any]]:
        for regex, source, _neg in group.patterns:
            found = regex.search(match_str)
            if found:
                return {"matched": found.group(0), "source": source,
                        "start": self._word_index(match_str, found.start()),
                        "end": self._word_index(match_str, found.end())}
        return None

    def _is_medical_question(self, match_str: str) -> bool:
        symptom = self._find_raw(self._medical_symptoms, match_str)
        if not symptom:
            return False
        return bool(self._find_raw(self._medical_questions, match_str)
                    or self._find_raw(self._medical_advice, match_str))

    def _context_medical(self, tokens: Sequence[str], context: dict | None) -> bool:
        """Вопрос об значениях анализов внутри сценария С-08 — тоже медицинский.

        Контекст передаёт движок диалога: ``{"scenario": "С-08"}``. Без контекста
        нейтральное «а там всё нормально?» флагом не считается.
        """
        if not isinstance(context, dict):
            return False
        hint_parts: list[str] = []
        for key in ("scenario", "step", "intent", "topic", "state"):
            try:
                value = context.get(key)
            except Exception:  # pragma: no cover
                value = None
            if value:
                hint_parts.append(str(value).lower())
        hint = " ".join(hint_parts)
        if not any(marker in hint for marker in ("с-08", "lab", "анализ", "результат")):
            return False
        prefixes = ("нормальн", "значени", "показател", "опасн", "расшифр", "холестерин", "сахар", "гемоглобин")
        exact = {"хорошие", "хороший", "хорошая", "плохие", "плохой", "плохая", "норма"}
        for word in tokens:
            if word in exact or any(word.startswith(prefix) for prefix in prefixes):
                return True
        return False

    def _is_third_party_request(self, match_str: str, low: str, tokens: Sequence[str]) -> bool:
        question = bool(self._find_raw(self._third_questions, match_str))
        state = bool(self._find_raw(self._third_states, match_str))
        if not (question and state):
            return False
        if self._find_raw(self._third_persons, match_str):
            return True
        for regex in self._third_name_res:
            if regex.search(match_str):
                return True
        return False

    def boundary_phrase(self, topic: str = "") -> str:
        """Граничная фраза для медицинского вопроса. Без препаратов и значений."""
        try:
            key = str(topic or "").replace("ё", "е").lower()
            for markers, name in _BOUNDARY_TOPIC_KEYS:
                if any(marker in key for marker in markers):
                    return BOUNDARY_PHRASES[name]
        except Exception:  # pragma: no cover
            pass
        return BOUNDARY_PHRASES["default"]

    # -- помощники для движка диалога --------------------------------------

    def all_matches(self, text: str) -> list[SafetyMatch]:
        """Все срабатывания по реплике — для панели демо «что понял ассистент»."""
        found: list[SafetyMatch] = []
        try:
            raw, low, tokens, bounds = self._prepare(text)
            match_str = " ".join(tokens)
            silence = self._silence_match(raw, tokens)
            if silence is not None:
                return [silence]
            soft, extra = self._soft_info(low, tokens)
            for group in self._cat_a:
                hit = self._find_in_group(group, match_str, tokens, bounds)
                if hit:
                    found.append(self._verdict(SafetyVerdict.RED_FLAG_A, group.name, self._rules["a"],
                                               f"soft={soft}; «{hit['matched']}»; {extra}", matched=hit["matched"]))
                    break
            for group in self._cat_b:
                hit = self._find_in_group(group, match_str, tokens, bounds)
                if hit:
                    found.append(self._verdict(SafetyVerdict.RED_FLAG_B, group.name, self._rules["b"],
                                               f"soft={soft}; «{hit['matched']}»; {extra}", matched=hit["matched"]))
                    break
            for group, verdict, rule, category in (
                (self._operator, SafetyVerdict.OPERATOR, self._rules["operator"], "Просьба соединить с человеком"),
                (self._complaint, SafetyVerdict.COMPLAINT, self._rules["complaint"], "Жалоба, конфликт"),
                (self._spd, SafetyVerdict.SPD_CONSENT, self._rules["spd_consent"], "Отзыв согласия на обработку ПДн"),
                (self._opt_out, SafetyVerdict.RECORD_OPT_OUT, self._rules["record_opt_out"], "Отказ от записи разговора"),
                (self._third, SafetyVerdict.THIRD_PARTY, self._rules["third_party"], "Запрос о другом человеке"),
                (self._medical, SafetyVerdict.MEDICAL, self._rules["medical"], "Медицинский вопрос"),
            ):
                hit = self._find_in_group(group, match_str, tokens, bounds)
                if hit:
                    found.append(self._verdict(verdict, category, rule, f"«{hit['matched']}»", matched=hit["matched"]))
            if not found:
                found.append(self._verdict(SafetyVerdict.OK, "", "", ""))
        except Exception:  # pragma: no cover
            return [self._verdict(SafetyVerdict.OK, "", "", "")]
        return found

    @staticmethod
    def is_soft(match: SafetyMatch) -> bool:
        """Мягкое срабатывание: фразу 103 произнести, но сценарий не рвать."""
        return "soft=True" in (match.reason or "")

    @staticmethod
    def route(match: SafetyMatch) -> str:
        """Адресат маршрутизации по разделу 7.2/7.3 (М0, М2, М3, ...)."""
        return ROUTES.get(match.verdict, ("", ""))[0]

    @staticmethod
    def priority(match: SafetyMatch) -> str:
        """Приоритет очереди переводов по разделу 7.5 (P1/P2/P3)."""
        return ROUTES.get(match.verdict, ("", ""))[1]

    def reply_hint(self, match: SafetyMatch) -> str:
        """Готовая реплика ассистента под вердикт (не длиннее 20 слов)."""
        try:
            if match.verdict is SafetyVerdict.RED_FLAG_A:
                return REPLY_RED_FLAG_A_SOFT if self.is_soft(match) else REPLY_RED_FLAG_A
            if match.verdict is SafetyVerdict.RED_FLAG_B:
                return REPLY_RED_FLAG_B
            if match.verdict is SafetyVerdict.OPERATOR:
                return REPLY_OPERATOR
            if match.verdict is SafetyVerdict.COMPLAINT:
                return REPLY_COMPLAINT
            if match.verdict is SafetyVerdict.SPD_CONSENT:
                return REPLY_SPD
            if match.verdict is SafetyVerdict.RECORD_OPT_OUT:
                return REPLY_RECORD_OPT_OUT
            if match.verdict is SafetyVerdict.SILENCE:
                return REPLY_SILENCE
            if match.verdict is SafetyVerdict.THIRD_PARTY:
                return REPLY_THIRD_PARTY
            if match.verdict is SafetyVerdict.MEDICAL:
                return self.boundary_phrase(f"{match.matched} {match.category}")
        except Exception:  # pragma: no cover
            return ""
        return ""

    def stats(self) -> dict[str, Any]:
        """Сколько правил и групп загружено — для отчёта и панели демо."""
        a_patterns = sum(len(group.patterns) for group in self._cat_a)
        b_patterns = sum(len(group.patterns) for group in self._cat_b)
        return {
            "glossary": self._loaded_from,
            "category_a_groups": len([g for g in self._cat_a if not g.is_empty]),
            "category_a_rules": a_patterns,
            "category_b_groups": len([g for g in self._cat_b if not g.is_empty]),
            "category_b_rules": b_patterns,
            "total_groups": len([g for g in self._cat_a + self._cat_b if not g.is_empty]) + 8,
            "total_rules": a_patterns + b_patterns + sum(
                len(group.patterns) for group in (
                    self._operator, self._operator_weak, self._complaint, self._medical,
                    self._medical_symptoms, self._spd, self._opt_out, self._third,
                    self._third_questions, self._third_states, self._third_persons,
                )
            ),
        }


# --------------------------------------------------------------------------
# Загрузка словаря
# --------------------------------------------------------------------------


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _load_glossary(path_str: str) -> tuple[dict[str, Any], str]:
    candidates: list[Path] = []
    if path_str:
        candidates.append(Path(path_str))
        candidates.append(_MODULE_DIR / Path(path_str).name)
    candidates.append(_MODULE_DIR / "red_flags.json")
    for candidate in candidates:
        try:
            if candidate.is_file():
                with candidate.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    return _deep_merge(_FALLBACK_GLOSSARY, data), str(candidate)
        except Exception:
            continue
    return _FALLBACK_GLOSSARY, "встроенный словарь"


__all__ = [
    "SafetyLayer", "DEFAULT_GLOSSARY_PATH",
    "REPLY_RED_FLAG_A", "REPLY_RED_FLAG_A_SOFT", "REPLY_RED_FLAG_B", "REPLY_OPERATOR",
    "REPLY_COMPLAINT", "REPLY_SPD", "REPLY_RECORD_OPT_OUT", "REPLY_SILENCE",
    "REPLY_THIRD_PARTY", "BOUNDARY_PHRASES", "ROUTES",
]

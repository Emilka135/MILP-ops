"""Conservative patient identity normalization and read-only exact DB lookup.

Surname/phone are discovery fields, never authentication. A complete exact card
or SNILS is required for success. Reviewed aliases permit foreign spellings;
there is deliberately no fuzzy matching, automatic transliteration or ID switch.
"""

import re
import unicodedata
from datetime import date

from calls.nlu.normalize import Num, numberize
from calls.tts.stress import require_stress_marks

MAX_CANDIDATES = 20
ALIAS_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_name_aliases (
 patient_id INTEGER NOT NULL REFERENCES patients(id), alias TEXT NOT NULL,
 approved INTEGER NOT NULL DEFAULT 0 CHECK(approved IN (0,1)),
 source TEXT NOT NULL, PRIMARY KEY(patient_id,alias));
"""
# This module does not apply the schema or create aliases. Administration/import
# must separately approve them. Both query variants are fixed, bound templates.
BASE_SQL = """SELECT p.id,p.first_name,p.middle_name FROM patients p
 WHERE (:card_no IS NULL OR p.card_no=:card_no)
 AND (:snils IS NULL OR p.snils=:snils)
 AND (:phone IS NULL OR p.phone=:phone)
 AND (:birth_date IS NULL OR p.birth_date=:birth_date)
 AND (:surname IS NULL OR NAME_MATCH)
 ORDER BY p.id LIMIT :limit"""
SQL_EXACT = BASE_SQL.replace("NAME_MATCH", "clinic_identity_name(p.last_name)=:surname")
SQL_ALIASES = BASE_SQL.replace("NAME_MATCH", """clinic_identity_name(p.last_name)=:surname
 OR EXISTS (SELECT 1 FROM identity_name_aliases a
 WHERE a.patient_id=p.id AND a.approved=1
 AND clinic_identity_name(a.alias)=:surname)""")

DIGIT_WORDS = {word: str(number) for number, word in enumerate(
    ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"))}
DIGIT_WORDS.update({"нуль": "0", "одна": "1", "две": "2"})
MONTHS = {word: month for month, forms in enumerate((
    "январь января", "февраль февраля", "март марта", "апрель апреля", "май мая", "июнь июня",
    "июль июля", "август августа", "сентябрь сентября", "октябрь октября", "ноябрь ноября", "декабрь декабря"), 1)
    for word in forms.split()}
PUNCTUATION = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'",
                            "‐": "-", "‑": "-", "–": "-", "—": "-"})


def text_value(value, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid_identity_text")
    return unicodedata.normalize("NFKC", value).translate(PUNCTUATION).strip()


def normalize_surname(value):
    text = text_value(value, 160).casefold().replace("ё", "е")
    text = re.sub(r"^(?:моя фамилия|фамилия)\s*:?\s+", "", text)
    text = text.rstrip(".!?").strip()
    text = text.replace(", ", " ")  # ASR often punctuates a compound surname as two words.
    if not text or any(not (unicodedata.category(char).startswith("L") or char in " '-") for char in text):
        raise ValueError("invalid_surname")
    # Punctuation has no acoustic realization. Preserve word boundaries and
    # every letter; equivalent punctuation can still produce ambiguity in DB.
    return " ".join(text.replace("'", "").replace("-", " ").split())


def normalize_identifier(value, field="card_no"):
    if field not in {"card_no", "snils", "phone", "birth_digits"}:
        raise ValueError("invalid_identifier_field")
    text = text_value(value).casefold().replace("ё", "е").rstrip(".!?").strip()
    text = re.sub(r"^(?:номер карты|номер снилс|номер телефона)\s*:?\s+", "", text)
    text = re.sub(r"(?<!\S)[,.;!?]+|[,.;!?]+(?=\s|$)", " ", text).strip()
    if re.fullmatch(r"[0-9\s-]+", text):
        digits = re.sub(r"[\s-]", "", text)
    elif field == "phone" and re.fullmatch(r"\+?[0-9()\s-]+", text):
        digits = re.sub(r"[+()\s-]", "", text)
    else:
        tokens = text.split()
        if not tokens or any(token not in DIGIT_WORDS for token in tokens):
            raise ValueError("say_identifier_digit_by_digit")
        digits = "".join(DIGIT_WORDS[token] for token in tokens)
    if field == "phone":
        if len(digits) == 10:
            digits = "7" + digits
        if len(digits) == 11 and digits[0] == "8":
            digits = "7" + digits[1:]
        valid = len(digits) == 11 and digits.startswith("7")
    else:
        valid = {"card_no": 3 <= len(digits) <= 10,
                 "snils": len(digits) == 11,
                 "birth_digits": len(digits) == 8}[field]
    if not valid:
        raise ValueError("incomplete_identifier")
    return digits


def _number_items(tokens):
    # Do not call generic tokenize(): its fuzzy repairs are inappropriate for
    # identity fields. Only literal, fully recognized number words are allowed.
    items = numberize(tokens)
    if not items or any(not isinstance(item, Num) for item in items):
        raise ValueError("invalid_date_number_words")
    return [item.value for item in items]


def normalize_birth_date(value, *, today=None, minimum=date.min):
    today = today or date.today()
    text = text_value(value).casefold().replace("ё", "е").rstrip(".!?").strip()
    text = re.sub(r"^(?:дата рождения|я родился|я родилась)\s*:?\s+", "", text)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        result = date.fromisoformat(text)
    elif re.fullmatch(r"[0-9]{2}[./][0-9]{2}[./][0-9]{4}", text):
        day, month, year = map(int, re.split(r"[./]", text))
        result = date(year, month, day)
    else:
        # ASR mixes ordinal words with numeric month/year and punctuation.
        numeric_text = re.sub(r"[.,/]", " ", text)
        numeric_text = re.sub(r"\s+(?:года|год|г)$", "", numeric_text).strip()
        items = numberize(numeric_text.split())
        if len(items) == 5 and all(isinstance(item, Num) for item in items) and items[0].value==0 and items[2].value==0:
            items=[Num(items[1].value),Num(items[3].value),items[4]]
        if len(items) == 3 and all(isinstance(item, Num) for item in items):
            day, month, year = [item.value for item in items]
            if year < 1000:raise ValueError("birth_year_required")
            result = date(year, month, day)
            if result > today:raise ValueError("birth_date_in_future")
            if result < minimum:raise ValueError("birth_date_before_minimum")
            return result
        month_tokens = re.findall(r"[а-я]+", text)
        found_months = [token for token in month_tokens if token in MONTHS]
        if found_months:
            if len(found_months) != 1 or not re.fullmatch(r"[а-я0-9\s]+", text):
                raise ValueError("ambiguous_birth_date")
            month_name = found_months[0]
            before, after = text.split(month_name)
            day_values = _number_items(before.split())
            if len(day_values) != 1:
                raise ValueError("ambiguous_birth_day")
            after = re.sub(r"\s+(?:года|год|г)$", "", after.strip())
            year_values = _number_items(after.split())
            year = year_values[0]
            if not 1000 <= year <= 9999:
                raise ValueError("birth_year_required")
            # numberize may split 1900 + ordinal 90. Permit only descending
            # place values, not repeated thousands or arbitrary added dates.
            last = year
            for extra in year_values[1:]:
                place = 1
                while last % (place * 10) == 0 and place < 1000:
                    place *= 10
                if not 0 < extra < place:
                    raise ValueError("ambiguous_birth_year")
                year += extra
                last = extra
            result = date(year, MONTHS[month_name], day_values[0])
        else:
            digits = normalize_identifier(text, "birth_digits")
            result = date(int(digits[4:]), int(digits[2:4]), int(digits[:2]))
    if result > today:
        raise ValueError("birth_date_in_future")
    if result < minimum:
        raise ValueError("birth_date_before_minimum")
    return result



def birth_date_input(value, *, today=None, minimum=date.min):
    """Field contract: Gregorian calendar, inclusive bounds, no age/century guess."""
    today = today or date.today()
    bounds = {'type':'date', 'purpose':'birth_date', 'minimum':minimum.isoformat(),
              'maximum':today.isoformat(), 'year_digits':4, 'century_inference':False}
    try:
        result=normalize_birth_date(value,today=today,minimum=minimum)
        return {'status':'valid','value':result.isoformat(),'field':bounds}
    except (ValueError,TypeError) as exc:
        text=str(value).casefold().replace('ё','е')
        partial = bool(re.fullmatch(r'\s*\d{1,2}[./]\d{1,2}[./]\d{2}\s*[.!]?\s*',text))
        # A recognized partial year needs a century clarification, never a
        # moving 100-year window that would exclude centenarians.
        year_words = re.sub(r'\s+(?:года|год|г)[.!]?$', '', text).strip()
        month=next((m for m in MONTHS if re.search(r'\b'+m+r'\b',year_words)),None)
        if month:
            try:partial = partial or 0 <= _number_items(year_words.split(month)[1].split())[0] < 100
            except (ValueError,IndexError):pass
        items=numberize(re.sub(r'[.,/]', ' ',year_words).split())
        if items and all(isinstance(i,Num) for i in items):
            values=[i.value for i in items]
            partial=partial or (len(values)==3 and 0<=values[-1]<100) or (len(values)==5 and values[0]==0 and values[2]==0 and 0<=values[-1]<100)
        reason='birth_year_required' if partial else str(exc)
        message = ('Уточните год рождения полностью, с указанием века. Например: тысяча девятьсот девяносто шестой.' if partial else
                   'Дата рождения не может быть позже сегодняшней даты. Повторите день, месяц и полный год.' if reason=='birth_date_in_future' else
                   'Дата раньше нижней границы поля. Уточните дату или обратитесь к регистратору.' if reason=='birth_date_before_minimum' else
                   'Не удалось разобрать календарную дату. Назовите отдельно день, месяц и полный год рождения.')
        return {'status':'clarify','reason':reason,'message':message,'field':bounds}

def checked_pronunciation(surname, pronunciation=None):
    normalize_surname(surname)
    if pronunciation is None:
        raise ValueError("explicit_name_pronunciation_required")
    spoken = text_value(pronunciation).translate(PUNCTUATION)
    if not re.fullmatch(r"[А-Яа-яЁё+ '\-]+", spoken):
        raise ValueError("explicit_cyrillic_pronunciation_required")
    try:
        require_stress_marks(spoken)
    except ValueError:
        raise ValueError("unverified_name_pronunciation") from None
    return spoken


class IdentityLookup:
    """Internal read-only lookup; caller text never provides a SQL statement."""
    def __init__(self, registry):
        self.registry = registry
        def database_name(value):
            try:
                return normalize_surname(value)
            except ValueError:
                return None
        with registry._lock:
            registry._conn.create_function("clinic_identity_name", 1, database_name, deterministic=True)

    def lookup(self, args, *, locked_patient_id=None, today=None):
        allowed = {"surname", "card_no", "snils", "phone", "birth_date"}
        try:
            if not isinstance(args, dict) or set(args) - allowed:
                raise ValueError("invalid_identity_fields")
            if locked_patient_id is not None and (
                isinstance(locked_patient_id, bool) or not isinstance(locked_patient_id, int) or locked_patient_id <= 0
            ):
                raise ValueError("invalid_locked_patient")
            params = {key: None for key in allowed}
            for key, value in args.items():
                if value is None or value == "":
                    continue
                if key == "surname":
                    params[key] = normalize_surname(value)
                elif key == "birth_date":
                    params[key] = normalize_birth_date(value, today=today).isoformat()
                else:
                    params[key] = normalize_identifier(value, key)
        except (ValueError, TypeError):
            return "invalid", {"reason": "invalid_identity_fields", "patient_id": None}
        strong = params["card_no"] is not None or params["snils"] is not None
        if not strong and not params["surname"] and not params["phone"]:
            return "clarify", {"reason": "full_identifier_required", "candidate_count": None}
        has_aliases = bool(self.registry.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", ("identity_name_aliases",)))
        rows = self.registry.execute(SQL_ALIASES if has_aliases else SQL_EXACT,
                                     {**params, "limit": MAX_CANDIDATES + 1})
        count = len(rows)
        if not count:
            return "clarify", {"reason": "no_exact_match", "candidate_count": 0,
                               "next": "repeat_full_identifier_or_operator"}
        if count > 1 or not strong:
            return "clarify", {"reason": "ambiguous_identity" if count > 1 else "full_identifier_required",
                               "candidate_count": min(count, MAX_CANDIDATES),
                               "more_candidates": count > MAX_CANDIDATES}
        row = rows[0]
        if locked_patient_id is not None and row["id"] != locked_patient_id:
            return "mismatch", {"reason": "locked_identity_conflict", "patient_id": None}
        return "ok", {"patient_id": row["id"], "patient_name": " ".join(
            value for value in (row["first_name"], row["middle_name"]) if value),
                      "identity_basis": "exact_full_identifier",
                      "snils_checksum_checked": False}

"""Conservative, local availability constraints; never chooses or reserves a slot.

Weekdays use Python's Monday=0 convention. Bounds carry explicit inclusivity.
The clinic's primary regulation, Appendix А.3, supplies the only default windows:
morning 07:30–12:00, afternoon/after lunch 12:00–16:00, evening after 16:00.
An unsupported temporal phrase or contradiction requires clarification; callers
must not execute an availability query while ``needs_clarification`` is true.
"""
from __future__ import annotations

from datetime import date, timedelta
import re

_DAY_FORMS = (
    r"понедельник(?:а|у|ом|е|и|ам)?|monday",
    r"вторник(?:а|у|ом|е|и|ам)?|tuesday",
    r"сред(?:а|у|ы|е|ой|ам)|wednesday",
    r"четверг(?:а|у|ом|е|и|ам)?|thursday",
    r"пятниц(?:а|у|ы|е|ей|ам)|friday",
    r"суббот(?:а|у|ы|е|ой|ам)|saturday",
    r"воскресень(?:е|я|ю|ем|ям)|sunday",
)
_DAY = r"(?:" + "|".join(_DAY_FORMS) + r")"
_DAY_RE = re.compile(r"\b" + _DAY + r"\b")
_MONTHS = dict(zip(("января", "февраля", "марта", "апреля", "мая", "июня",
                    "июля", "августа", "сентября", "октября", "ноября", "декабря"), range(1, 13)))
_DATE = (r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}(?:\.\d{4})?|"
         r"\d{1,2}\s+(?:" + "|".join(_MONTHS) + r")(?:\s+\d{4}(?:\s+года)?)?|"
         r"послезавтра|завтра|сегодня|tomorrow|today)\b")
_DATE_RE = re.compile(r"(?<![\w.])" + _DATE)
_NEGATIVE_PREFIX = r"(?:(?:не|ни)\s+(?:(?:хочу|могу|смогу)\s+)?(?:(?:в|во|на|по)\s+)?|без\s+|кроме\s+|за\s+исключением\s+|исключая\s+|not\s+(?:on\s+)?|except\s+(?:on\s+)?)"
_JOIN = r"\s*(?:,|и|или|либо|and|or)\s*(?:(?:не\s+)?(?:в|во|на|по|on)\s+)?"
_NEG_DAYS = re.compile(r"\b" + _NEGATIVE_PREFIX + r"(?P<items>" + _DAY + r"(?:" + _JOIN + _DAY + r")*)\b")
_NEG_DATES = re.compile(r"\b" + _NEGATIVE_PREFIX + r"(?P<items>" + _DATE + r"(?:" + _JOIN + _DATE + r")*)")
_HOUR_WORDS = {
    "ноль": 0, "один": 1, "одного": 1, "двух": 2, "два": 2, "трех": 3, "три": 3,
    "четырех": 4, "четыре": 4, "пяти": 5, "пять": 5, "шести": 6, "шесть": 6,
    "семи": 7, "семь": 7, "восьми": 8, "восемь": 8, "девяти": 9, "девять": 9,
    "десяти": 10, "десять": 10, "одиннадцати": 11, "одиннадцать": 11,
    "двенадцати": 12, "двенадцать": 12, "тринадцати": 13, "тринадцать": 13,
    "четырнадцати": 14, "четырнадцать": 14, "пятнадцати": 15, "пятнадцать": 15,
    "шестнадцати": 16, "шестнадцать": 16, "семнадцати": 17, "семнадцать": 17,
    "восемнадцати": 18, "восемнадцать": 18, "девятнадцати": 19, "девятнадцать": 19,
}
_HOUR = r"(?:\d{1,2}(?::\d{2})?|двадцат[ьи](?:\s+(?:одного|один|двух|два|трех|три))?|" + "|".join(sorted(_HOUR_WORDS, key=len, reverse=True)) + r")"
_BOUND = re.compile(r"\b(?P<op>не\s+раньше|не\s+ранее|не\s+позже|не\s+позднее|"
                    r"not\s+before|not\s+after|после|раньше|позже|перед|до|с|в|at|after|before|from|until)"
                    r"\s+(?P<hour>" + _HOUR + r")(?P<qualifier>\s+(?:час(?:а|ов)?\s*)?(?:утра|дня|вечера|ночи))?\b")


def _weekday(text):
    return next(i for i, pattern in enumerate(_DAY_FORMS) if re.fullmatch(pattern, text))


def _overlaps(start, end, spans):
    return any(start < b and a < end for a, b in spans)


def parse(text: str, today: date) -> dict:
    """Return JSON-compatible constraints and source spans, without external I/O.

    Dates without a year require clarification: the regulation does not specify
    how to infer that year. An explicit numeric clock uses the 24-hour system;
    ambiguous spoken hours such as «после двух» need an AM/PM clarification.
    ``evidence`` offsets refer to the original string (ё normalization preserves
    length), so NLU can avoid treating a negated date as a positive preference.
    """
    if not isinstance(text, str) or not isinstance(today, date):
        raise TypeError('text must be str and today must be datetime.date')
    normalized = text.lower().replace('ё', 'е')
    result = {'excluded_weekdays': [], 'excluded_dates': [], 'weekdays': [], 'dates': [],
              'time_from': None, 'time_to': None, 'time_from_inclusive': True,
              'time_to_inclusive': True, 'needs_clarification': False, 'reasons': [], 'evidence': []}
    negated_spans = []
    consumed_times = []

    def clarify(reason):
        if reason not in result['reasons']:
            result['reasons'].append(reason)
        result['needs_clarification'] = True

    def evidence(field, value, start, end, rule):
        result['evidence'].append({'field': field, 'value': value, 'text': text[start:end],
                                   'start': start, 'end': end, 'rule': rule})

    def add(field, value, start, end, rule):
        if value not in result[field]:
            result[field].append(value)
        evidence(field, value, start, end, rule)

    def bound(field, minutes, inclusive, start, end, rule):
        value = f'{minutes // 60:02d}:{minutes % 60:02d}'
        current = result[field]
        tighter = current is None or (value > current if field == 'time_from' else value < current)
        if tighter:
            result[field], result[field + '_inclusive'] = value, inclusive
        elif current == value:
            result[field + '_inclusive'] &= inclusive
        evidence(field, value, start, end, rule)

    # Negation binds to an explicit list, never to an arbitrary distant noun.
    for expression, matcher, field in ((_NEG_DAYS, _DAY_RE, 'excluded_weekdays'),
                                       (_NEG_DATES, _DATE_RE, 'excluded_dates')):
        for group in expression.finditer(normalized):
            a, b = group.span('items')
            negated_spans.append((a, b))
            if field == 'excluded_weekdays':
                for hit in matcher.finditer(normalized, a, b):
                    add(field, _weekday(hit.group()), hit.start(), hit.end(), 'explicit_negation')

    postfix = re.compile(r'(?P<items>' + _DAY + r'(?:' + _JOIN + _DAY + r')*)\s+(?:(?:мне|нам)\s+)?не\s+(?:подход(?:ит|ят)|могу|смогу|удобно)')
    for group in postfix.finditer(normalized):
        a, b = group.span('items'); negated_spans.append((a, b))
        for hit in _DAY_RE.finditer(normalized, a, b):
            add('excluded_weekdays', _weekday(hit.group()), hit.start(), hit.end(), 'postfix_negation')

    nonexclusive = bool(re.search(r'\b(?:не\s+только|not\s+only)\b', normalized))
    for hit in _DAY_RE.finditer(normalized):
        if _overlaps(hit.start(), hit.end(), negated_spans):
            continue
        before, after = normalized[max(0, hit.start() - 35):hit.start()], normalized[hit.end():hit.end() + 35]
        if re.match(r'\s+(?:(?:мне|нам)\s+)?не\s+(?:подход(?:ит|ят)|могу|смогу|удобно)', after):
            add('excluded_weekdays', _weekday(hit.group()), hit.start(), hit.end(), 'postfix_negation')
        elif re.search(r'\b(?:до|после|раньше|позже|перед)\s+(?:этой\s+)?$', before):
            clarify('relative_weekday_range_not_supported')
        elif re.search(r'\bне\s+.*(?:эту|ближайшую|следующую)\s+$', before):
            clarify('particular_weekday_negation_needs_date')
        elif not nonexclusive:
            add('weekdays', _weekday(hit.group()), hit.start(), hit.end(), 'explicit_weekday')

    date_spans = []
    for hit in _DATE_RE.finditer(normalized):
        a, b = hit.span(); date_spans.append((a, b)); value = hit.group()
        try:
            if value in {'сегодня', 'today', 'завтра', 'tomorrow', 'послезавтра'}:
                parsed = today + timedelta(days={'сегодня': 0, 'today': 0, 'завтра': 1, 'tomorrow': 1, 'послезавтра': 2}[value])
            elif re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                parsed = date.fromisoformat(value)
            elif '.' in value:
                parts = value.split('.')
                if len(parts) != 3:
                    clarify('explicit_date_year_required'); continue
                day, month, year = map(int, parts); parsed = date(year, month, day)
            else:
                tokens = value.split()
                if len(tokens) < 3:
                    clarify('explicit_date_year_required'); continue
                parsed = date(int(tokens[2]), _MONTHS[tokens[1]], int(tokens[0]))
        except ValueError:
            clarify('invalid_date'); continue
        prefix = normalized[max(0, a-25):a]
        if re.search(r'\b(?:до|после|раньше|позже|before|after|с|from)\s*$', prefix):
            clarify('date_range_not_supported'); continue
        field = 'excluded_dates' if _overlaps(a, b, negated_spans) else 'dates'
        if nonexclusive and field == 'dates':
            continue
        add(field, parsed.isoformat(), a, b, 'explicit_date')

    # Source windows are not the clinic's lunch break or guessed opening hours.
    periods = ((r'\b(?:с\s+утра|утром|in\s+the\s+morning)\b', 450, 720, True),
               (r'\b(?:после\s+обеда|днем|во\s+второй\s+половине\s+дня|after\s+lunch|in\s+the\s+afternoon)\b', 720, 960, True),
               (r'\b(?:вечером|in\s+the\s+evening)\b', 960, None, False),
               (r'\b(?:до\s+обеда|before\s+lunch)\b', None, 720, False))
    for pattern, lower, upper, inclusive in periods:
        for hit in re.finditer(pattern, normalized):
            a, b = hit.span(); consumed_times.append((a, b))
            if re.search(r'\b(?:не|not)\s+$', normalized[max(0, a-10):a]):
                clarify('negated_daypart_not_supported'); continue
            if re.search(r'\b(?:раньше|позже|ранее|позднее)\s+$', normalized[max(0,a-20):a]):
                clarify('relative_daypart_bound_not_supported'); continue
            if nonexclusive and re.search(r'\b(?:не\s+только|not\s+only)\s+$', normalized[max(0, a-20):a]):
                continue
            if lower is not None:
                bound('time_from', lower, inclusive, a, b, 'regulation_appendix_A3')
            if upper is not None:
                bound('time_to', upper, inclusive, a, b, 'regulation_appendix_A3')

    for hit in _BOUND.finditer(normalized):
        a, b = hit.span()
        if _overlaps(a, b, date_spans) or _overlaps(a, b, consumed_times):
            continue
        if re.search(r'\b(?:не|not)\s+$', normalized[max(0, a-10):a]):
            clarify('negated_bound_not_supported'); continue
        if re.search(r'\b(?:не\s+только|not\s+only)\s+$', normalized[max(0, a-20):a]):
            continue
        if re.match(r'\s+(?:тридцать|пятнадцать|сорок|половин|quarter|thirty)\b', normalized[b:]):
            clarify('spoken_minutes_not_supported'); continue
        raw = hit['hour']; qualifier = (hit['qualifier'] or '').strip()
        if raw[0].isdigit():
            parts = raw.split(':'); hour = int(parts[0]); minute = int(parts[1]) if len(parts) == 2 else 0
        else:
            if raw.startswith('двадцат'):
                bits = raw.split(); hour = 20 + (_HOUR_WORDS[bits[1]] if len(bits) > 1 else 0)
            else:
                hour = _HOUR_WORDS[raw]
            minute = 0
            if 0 < hour < 12 and not qualifier:
                clarify('spoken_hour_needs_am_pm'); continue
        op = ' '.join(hit['op'].split())
        end_of_day = hour == 24 and minute == 0 and op in {'до', 'перед', 'раньше', 'не позже', 'не позднее', 'not after', 'before', 'until'}
        if (hour > 23 and not end_of_day) or minute > 59:
            clarify('invalid_time'); continue
        if qualifier:
            if hour > 12:
                clarify('inconsistent_time_qualifier'); continue
            if re.search(r'дня|вечера', qualifier):
                hour = hour + 12 if hour < 12 else hour
            elif 'ночи' in qualifier and hour == 12:
                hour = 0
        minutes = hour * 60 + minute
        lower = op in {'после', 'позже', 'не раньше', 'не ранее', 'not before', 'after', 'from', 'с'}
        inclusive = op in {'не раньше', 'не ранее', 'не позже', 'не позднее', 'not before', 'not after', 'from', 'с'}
        if op in {'в', 'at'}:
            bound('time_from', minutes, True, a, b, 'explicit_clock_time')
            bound('time_to', minutes, True, a, b, 'explicit_clock_time')
        else:
            bound('time_from' if lower else 'time_to', minutes, inclusive, a, b, 'explicit_clock_bound')
        consumed_times.append((a, b))

    # These phrases have no single supported clock/date interpretation.
    if len(consumed_times) > 1:
        a = min(span[0] for span in consumed_times); b = max(span[1] for span in consumed_times)
        if re.search(r'\b(?:или|либо|or)\b', normalized[a:b]):
            clarify('disjoint_time_windows')
    if re.search(r'\b(?:с|со|from)\s+' + _DAY + r'\s+(?:по|до|to|through)\s+' + _DAY, normalized):
        clarify('weekday_range_not_supported')
    if re.search(r'\b(?:около|примерно|к\s+обеду|в\s+обед|после\s+перерыва|между|around|about\s+\d|noon)\b', normalized):
        clarify('ambiguous_or_unsupported_time')
    if re.search(r'\b(?:не\s+раньше|не\s+позже|после|до|before|after)\s+(?:\d|[а-я]+)', normalized):
        for hit in re.finditer(r'\b(?:не\s+раньше|не\s+позже|после|до|before|after)\s+\S+', normalized):
            if not _overlaps(hit.start(), hit.end(), consumed_times + date_spans) and not re.search(_DAY, hit.group()):
                clarify('unsupported_temporal_bound')
    if re.search(r'\b(?:не\s+не|не\s+без|not\s+not)\b', normalized):
        clarify('double_negation')
    if re.search(r'\b(?:утром|вечером|после\s+обеда|до\s+обеда)\s+или\s+(?:утром|вечером|после\s+обеда|до\s+обеда)\b', normalized):
        clarify('disjoint_time_windows')
    if len(set(result['excluded_weekdays'])) == 7:
        clarify('all_weekdays_excluded')
    if set(result['weekdays']) & set(result['excluded_weekdays']):
        clarify('weekday_included_and_excluded')
    if set(result['dates']) & set(result['excluded_dates']):
        clarify('date_included_and_excluded')
    for value in result['dates']:
        weekday = date.fromisoformat(value).weekday()
        if weekday in result['excluded_weekdays'] or (result['weekdays'] and weekday not in result['weekdays']):
            clarify('date_conflicts_with_weekday')
        if date.fromisoformat(value) < today:
            clarify('appointment_date_in_past')
    lower, upper = result['time_from'], result['time_to']
    if lower is not None and upper is not None and (lower > upper or (lower == upper and not (result['time_from_inclusive'] and result['time_to_inclusive']))):
        clarify('empty_time_window')
    for key in ('excluded_weekdays', 'excluded_dates', 'weekdays', 'dates'):
        result[key].sort()
    return result


def validate(spec):
    """Validate the entire internal filter before generating SQL or committing."""
    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise ValueError('invalid_availability_constraints')
    keys = {'excluded_weekdays', 'excluded_dates', 'weekdays', 'dates', 'time_from', 'time_to',
            'time_from_inclusive', 'time_to_inclusive', 'needs_clarification', 'reasons', 'evidence'}
    if set(spec) - keys:
        raise ValueError('unknown_availability_constraint')
    if type(spec.get('needs_clarification', False)) is not bool or spec.get('needs_clarification'):
        raise ValueError('availability_needs_clarification')
    result = {}
    for field in ('excluded_weekdays', 'weekdays', 'excluded_dates', 'dates'):
        values = spec.get(field, [])
        if not isinstance(values, list) or len(values) > (7 if 'weekdays' in field else 31):
            raise ValueError('invalid_availability_list')
        for value in values:
            if 'weekdays' in field:
                if type(value) is not int or not 0 <= value <= 6:
                    raise ValueError('invalid_weekday')
            elif not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError('invalid_availability_date')
            else:
                date.fromisoformat(value)
        result[field] = sorted(set(values))
    for field in ('time_from', 'time_to'):
        value = spec.get(field)
        if value is not None and (not isinstance(value, str) or not (
                re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value) or field == 'time_to' and value == '24:00')):
            raise ValueError('invalid_availability_time')
        result[field] = value
        inclusive = spec.get(field + '_inclusive', True)
        if type(inclusive) is not bool:
            raise ValueError('invalid_bound_inclusivity')
        result[field + '_inclusive'] = inclusive
    if set(result['weekdays']) & set(result['excluded_weekdays']) or len(result['excluded_weekdays']) == 7:
        raise ValueError('contradictory_weekdays')
    if set(result['dates']) & set(result['excluded_dates']):
        raise ValueError('contradictory_dates')
    for value in result['dates']:
        day = date.fromisoformat(value).weekday()
        if day in result['excluded_weekdays'] or result['weekdays'] and day not in result['weekdays']:
            raise ValueError('date_conflicts_with_weekday')
    lower, upper = result['time_from'], result['time_to']
    if lower is not None and upper is not None and (lower > upper or lower == upper and not (result['time_from_inclusive'] and result['time_to_inclusive'])):
        raise ValueError('empty_time_window')
    return result


def sql_filter(spec):
    """Only fixed column expressions/operators; every patient value is bound."""
    spec = validate(spec)
    clauses, parameters = [], {}
    for field, column, excluded in (
        ('weekdays', "((CAST(strftime('%w',s.starts_at) AS INTEGER)+6)%7)", False),
        ('excluded_weekdays', "((CAST(strftime('%w',s.starts_at) AS INTEGER)+6)%7)", True),
        ('dates', 'substr(s.starts_at,1,10)', False),
        ('excluded_dates', 'substr(s.starts_at,1,10)', True),
    ):
        values = spec.get(field, [])
        if values:
            placeholders = []
            for index, value in enumerate(values):
                key = f'av_{field}_{index}'
                placeholders.append(':' + key)
                parameters[key] = value
            clauses.append(column + (' NOT IN (' if excluded else ' IN (') + ','.join(placeholders) + ')')
    for field, greater in (('time_from', True), ('time_to', False)):
        if spec.get(field) is not None:
            operator = ('>' if greater else '<') + ('=' if spec[field + '_inclusive'] else '')
            clauses.append('substr(s.starts_at,12,5)' + operator + ':' + 'av_' + field)
            parameters['av_' + field] = spec[field]
    return ' AND '.join(clauses) or '1=1', parameters


def matches(spec, starts_at):
    """Validate the final slot independently of SQL query construction."""
    from datetime import datetime
    spec = validate(spec)
    parsed = datetime.fromisoformat(str(starts_at))
    day, weekday, clock = parsed.date().isoformat(), parsed.weekday(), parsed.strftime('%H:%M')
    if weekday in spec.get('excluded_weekdays', []) or day in spec.get('excluded_dates', []):
        return False
    if spec.get('weekdays') and weekday not in spec['weekdays'] or spec.get('dates') and day not in spec['dates']:
        return False
    lower, upper = spec.get('time_from'), spec.get('time_to')
    if lower is not None and (clock < lower or clock == lower and not spec['time_from_inclusive']):
        return False
    if upper is not None and (clock > upper or clock == upper and not spec['time_to_inclusive']):
        return False
    return True

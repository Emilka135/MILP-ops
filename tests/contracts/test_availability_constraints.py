"""Independent negative-preference contracts, including source D06 and A.3."""
from datetime import date
import json

import pytest

from clinic.availability_constraints import parse

TODAY = date(2026, 10, 21)


def constraints(text):
    value = parse(text, today=TODAY)
    assert json.loads(json.dumps(value, ensure_ascii=False)) == value
    for item in value['evidence']:
        assert item['text'] == text[item['start']:item['end']]
    return value


def test_d06_negative_friday_and_regulation_afternoon_window():
    c = constraints('Мне к лору, но лучше не в пятницу, после обеда')
    assert c['excluded_weekdays'] == [4]
    assert c['dates'] == [] and c['weekdays'] == []
    assert (c['time_from'], c['time_to']) == ('12:00', '16:00')
    assert c['needs_clarification'] is False
    assert any(e['field'] == 'excluded_weekdays' and e['text'] == 'пятницу' for e in c['evidence'])


@pytest.mark.parametrize('text,expected', [
    ('не в пятницу и субботу', [4, 5]),
    ('ни в пятницу ни в субботу', [4, 5]),
    ('без среды и пятницы', [2, 4]),
    ('пятница и суббота мне не подходят', [4, 5]),
    ('кроме среды и пятницы', [2, 4]),
    ('не в пятницу, не в субботу', [4, 5]),
    ('пятница мне не подходит', [4]),
    ('not on Friday or Saturday', [4, 5]),
])
def test_explicit_weekday_negation_lists(text, expected):
    c = constraints(text)
    assert c['excluded_weekdays'] == expected
    assert c['weekdays'] == []
    assert not c['needs_clarification']


@pytest.mark.parametrize('text', ['не только в пятницу', 'не только в пятницу, но и в субботу', 'not only on Friday'])
def test_not_only_is_not_a_ban_or_exclusive_preference(text):
    c = constraints(text)
    assert c['excluded_weekdays'] == [] and c['weekdays'] == []
    assert not c['needs_clarification']


def test_not_only_does_not_hide_an_independent_negative_constraint():
    c = constraints('не только в пятницу, но не в субботу')
    assert c['excluded_weekdays'] == [5] and c['weekdays'] == []


@pytest.mark.parametrize('text,lower,upper,lower_inclusive,upper_inclusive', [
    ('не раньше 12:00 и не позже 16:00', '12:00', '16:00', True, True),
    ('после 12:00 и до 16:00', '12:00', '16:00', False, False),
    ('after 12:00 and before 16:00', '12:00', '16:00', False, False),
    ('not before 12:00 and not after 16:00', '12:00', '16:00', True, True),
    ('с утра', '07:30', '12:00', True, True),
    ('днём', '12:00', '16:00', True, True),
    ('after lunch', '12:00', '16:00', True, True),
    ('вечером', '16:00', None, False, True),
    ('до обеда', None, '12:00', True, False),
    ('не раньше двенадцати', '12:00', None, True, True),
    ('после двух дня', '14:00', None, False, True),
    ('после двадцати трех', '23:00', None, False, True),
    ('в 14:30', '14:30', '14:30', True, True),
    ('после обеда, не раньше 14:00', '14:00', '16:00', True, True),
    ('с утра, не раньше 10:00', '10:00', '12:00', True, True),
    ('не раньше 12:00 и не позже 12:00', '12:00', '12:00', True, True),
])
def test_bounds_preserve_direction_and_source_windows(text, lower, upper, lower_inclusive, upper_inclusive):
    c = constraints(text)
    assert (c['time_from'], c['time_to']) == (lower, upper)
    assert (c['time_from_inclusive'], c['time_to_inclusive']) == (lower_inclusive, upper_inclusive)
    assert not c['needs_clarification'], c


@pytest.mark.parametrize('text,dates,excluded', [
    ('в 24.10.2026', ['2026-10-24'], []),
    ('не 24.10.2026', [], ['2026-10-24']),
    ('кроме 24.10.2026 и 25.10.2026', [], ['2026-10-24', '2026-10-25']),
    ('не 24 октября 2026 года', [], ['2026-10-24']),
    ('не завтра, а послезавтра', ['2026-10-23'], ['2026-10-22']),
    ('не 2026-10-24, а 2026-10-25', ['2026-10-25'], ['2026-10-24']),
])
def test_explicit_and_negative_dates_are_separate(text, dates, excluded):
    c = constraints(text)
    assert c['dates'] == dates and c['excluded_dates'] == excluded
    assert not c['needs_clarification'], c


def test_positive_day_does_not_turn_into_a_chosen_slot():
    c = constraints('не в пятницу, а в субботу после обеда')
    assert c['excluded_weekdays'] == [4] and c['weekdays'] == [5]
    assert c['dates'] == []
    assert not c['needs_clarification']


@pytest.mark.parametrize('text,reason', [
    ('после 17:00 и до 12:00', 'empty_time_window'),
    ('после 12:00 и до 12:00', 'empty_time_window'),
    ('не в пятницу, только в пятницу', 'weekday_included_and_excluded'),
    ('24.10.2026, но не 24.10.2026', 'date_included_and_excluded'),
    ('не в пятницу, но 23.10.2026', 'date_conflicts_with_weekday'),
    ('в субботу 23.10.2026', 'date_conflicts_with_weekday'),
    ('31.02.2026', 'invalid_date'),
    ('24.10', 'explicit_date_year_required'),
    ('24 октября', 'explicit_date_year_required'),
    ('20.10.2026', 'appointment_date_in_past'),
    ('после 25:00', 'invalid_time'),
    ('не раньше 14:77', 'invalid_time'),
    ('после двух', 'spoken_hour_needs_am_pm'),
    ('около обеда', 'ambiguous_or_unsupported_time'),
    ('в обед', 'ambiguous_or_unsupported_time'),
    ('после перерыва', 'ambiguous_or_unsupported_time'),
    ('между двенадцатью и часом', 'ambiguous_or_unsupported_time'),
    ('не утром', 'negated_daypart_not_supported'),
    ('не не в пятницу', 'double_negation'),
    ('после пятницы', 'relative_weekday_range_not_supported'),
    ('не в эту пятницу', 'particular_weekday_negation_needs_date'),
    ('утром или вечером', 'disjoint_time_windows'),
    ('после 12:00 или после 18:00', 'disjoint_time_windows'),
    ('до 24.10.2026', 'date_range_not_supported'),
    ('не позже 24.10.2026', 'date_range_not_supported'),
    ('не после 12:00', 'negated_bound_not_supported'),
    ('не до 12:00', 'negated_bound_not_supported'),
    ('после двенадцати тридцать', 'spoken_minutes_not_supported'),
    ('not before lunch', 'negated_daypart_not_supported'),
    ('не раньше после обеда', 'relative_daypart_bound_not_supported'),
    ('с понедельника по пятницу', 'weekday_range_not_supported'),
])
def test_unsupported_or_conflicting_constraints_require_clarification(text, reason):
    c = constraints(text)
    assert c['needs_clarification'] and reason in c['reasons'], c


def test_unknown_medical_text_does_not_create_temporal_constraints():
    c = constraints('Хочу записаться к лору')
    assert c['excluded_weekdays'] == c['weekdays'] == c['excluded_dates'] == c['dates'] == []
    assert c['time_from'] is None and c['time_to'] is None
    assert not c['needs_clarification']


def test_no_days_is_a_conflict_not_a_silent_empty_result():
    c = constraints('кроме понедельника, вторника, среды, четверга, пятницы, субботы и воскресенья')
    assert c['excluded_weekdays'] == list(range(7))
    assert c['needs_clarification'] and 'all_weekdays_excluded' in c['reasons']

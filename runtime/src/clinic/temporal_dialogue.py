"""LLM fallback for misunderstood time preferences, with explicit patient confirmation.

No MIS access, booking IDs, patient identity or write tools cross this boundary.
The ordinary parser handles understood phrases without a generative request.
"""
from datetime import date
import re

from .availability_constraints import parse, validate
from .identity_form import DialogueModel
from .module_settings import configured

QUESTION = 'Какое время вам удобно? Можно назвать часть дня или временной интервал.'
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'needs_clarification': {'type': 'boolean'},
        'time_from': {'type': ['string', 'null']},
        'time_to': {'type': ['string', 'null']},
        'time_from_inclusive': {'type': 'boolean'},
        'time_to_inclusive': {'type': 'boolean'},
        'evidence': {'type': 'string'},
        'question': {'type': 'string'},
    },
    'required': ['needs_clarification', 'time_from', 'time_to', 'time_from_inclusive',
                 'time_to_inclusive', 'evidence', 'question'],
}


def safe_question(value):
    return (isinstance(value, str) and 10 <= len(value) <= 220 and '?' in value
            and not re.search(r'[{}<>\x00-\x1f]|записан|отмен|подтвержден|паспорт|полис|смс|лекар|диагноз|https?://', value, re.I))


class TemporalDialogue:
    def __init__(self, model=None):
        self.model = model or DialogueModel(configured())

    def resolve(self, utterance='', constraints=None, today=None, **_):
        text = str(utterance or '')[:1000]
        current = parse(text, date.fromisoformat(today) if today else date.today())
        # Retain independently parsed date/weekday preferences from earlier turns.
        base = dict(constraints or {})
        for key, value in current.items():
            if value not in (None, [], ''):
                base[key] = sorted(set(base.get(key, [])) | set(value)) if key in {'excluded_dates', 'excluded_weekdays'} else value
        parsed = None
        if not current['needs_clarification'] and current['evidence']:
            try:
                parsed = validate(current)
            except ValueError:
                pass
        source = 'parser'
        if parsed is None:
            source = 'llm'
            task = {
                'task': ('Разбери только удобное время из реплики пациента и контекста. '
                         'Верни границы HH:MM, включительность и дословную цитату evidence. '
                         'Часть дня может быть выражена словами. До полуночи — верхняя граница 24:00 '
                         'конца того же дня, а не 00:00 его начала. Стандартные части дня по регламенту: '
                         'утром 07:30–12:00, днем/после обеда 12:00–16:00, вечером после 16:00. '
                         'Явная граница заменяет типовую границу части дня. '
                         'Например, «после двух часов дня и до полуночи»: time_from=14:00, '
                         'time_to=24:00, обе границы исключительные, needs_clarification=false. '
                         'Не угадывай время после работы или личного обеденного перерыва пациента: '
                         'needs_clarification=true и один короткий вопрос. '
                         'Не меняй даты, специальность, личность, записи. Никаких инструментов МИС.'),
                'utterance': text,
            }
            try:
                answer = self.model.complete(task, SCHEMA)
            except (OSError, ValueError, TypeError, KeyError):
                answer = None
            if not isinstance(answer, dict) or set(answer) != set(SCHEMA['required']):
                return self.clarify(QUESTION, 'model_unavailable_or_invalid')
            if type(answer['needs_clarification']) is not bool:
                return self.clarify(QUESTION, 'invalid_model_decision')
            if answer['needs_clarification']:
                return self.clarify(answer['question'] if safe_question(answer['question']) else QUESTION, 'model_needs_detail')
            evidence = answer['evidence']
            if not isinstance(evidence, str) or not evidence.strip() or evidence.casefold() not in text.casefold():
                return self.clarify(QUESTION, 'model_evidence_missing')
            parsed = {key: answer[key] for key in ('time_from', 'time_to', 'time_from_inclusive', 'time_to_inclusive')}
            try:
                validate(parsed)
            except (TypeError, ValueError):
                return self.clarify(QUESTION, 'invalid_time_proposal')
            # Numeric validity is insufficient: a model must not invent the
            # patient's work/lunch end. Ground each proposed clock in explicit
            # clocks, the regulation's daypart or an unambiguous midnight anchor.
            anchors = {item['value'] for item in current['evidence'] if item['field'] in {'time_from', 'time_to'}}
            if re.search(r'\bполуноч[ьи]\b', text, re.I):
                anchors.add('24:00')
            if any(value is not None and value not in anchors for key, value in parsed.items() if key in {'time_from', 'time_to'}):
                task = {
                    'task': 'Сформулируй один короткий вопрос о неизвестной границе удобного времени. '
                            'Пациент не сообщил время окончания работы или другого личного события. '
                            'Спроси недостающее время, не предлагай часы от себя и не сообщай о записи.',
                    'utterance': text,
                }
                try:
                    question = self.model.complete(task, {'type':'object','properties':{'question':{'type':'string'}},'required':['question'],'additionalProperties':False})
                except (OSError, ValueError, TypeError, KeyError):
                    question = None
                question = question.get('question') if isinstance(question, dict) else None
                return self.clarify(question if safe_question(question) else QUESTION, 'ungrounded_time_needs_detail')
            for item in current['evidence']:
                field = item['field']
                if item['rule'].startswith('explicit_clock') and field in {'time_from', 'time_to'} and parsed[field] == item['value']:
                    parsed[field + '_inclusive'] = current[field + '_inclusive']
            if parsed['time_to'] == '24:00':
                parsed['time_to_inclusive'] = False
        # The model is allowed to interpret time, not silently remove calendar bans.
        merged = {key: base[key] for key in ('weekdays', 'excluded_weekdays', 'dates', 'excluded_dates') if key in base}
        merged.update({key: parsed.get(key) for key in ('time_from', 'time_to', 'time_from_inclusive', 'time_to_inclusive')})
        try:
            proposal = validate(merged)
            if proposal['time_from'] is None and proposal['time_to'] is None:
                raise ValueError('missing_time_bounds')
        except (TypeError, ValueError):
            return self.clarify(QUESTION, 'invalid_time_proposal')
        bounds = []
        if proposal['time_from'] is not None:
            bounds.append(('начиная с ' if proposal['time_from_inclusive'] else 'после ') + proposal['time_from'])
        if proposal['time_to'] is not None:
            end = 'полуночи' if proposal['time_to'] == '24:00' else proposal['time_to']
            bounds.append(('не позже ' if proposal['time_to_inclusive'] else 'до ') + end)
        return 'proposal', {'availability_proposal': proposal, 'availability_resolution_source': source,
                            'availability_question': 'Правильно понимаю: вам удобно ' + ' и '.join(bounds) + '? Да — один. Нет — ноль.'}

    @staticmethod
    def clarify(question, reason):
        return 'clarify', {'availability_proposal': None, 'availability_question': question,
                           'availability_resolution_source': reason}

    @staticmethod
    def confirm(proposal=None, **_):
        try:
            spec = validate(proposal)
            if not spec or spec.get('time_from') is None and spec.get('time_to') is None:
                raise ValueError('empty_proposal')
        except (TypeError, ValueError):
            return 'error', {'error': 'invalid_time_proposal'}
        spec.update(needs_clarification=False, reasons=[], evidence=[])
        return 'ok', {'availability_constraints': spec, 'availability_proposal': None,
                      'availability_question': '', 'said_time': None, 'said_daypart': None}

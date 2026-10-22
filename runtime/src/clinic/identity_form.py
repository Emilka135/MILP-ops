"""Required-field form. Extraction is untrusted; only an exact DB match verifies identity.

The same form is usable from a browser call and a telephony subroutine. A model
may extract literal name spans and phrase a question; it cannot return a patient
ID, SQL, a birth date guessed from a partial year, or a booking decision.
"""
from dataclasses import dataclass, field
import json
import os
import re
import unicodedata
import urllib.request

from calls.services.netutil import open_http

NAME_FIELDS = ('last_name', 'first_name', 'middle_name')
NAME_LABELS = dict(last_name='фамилию', first_name='имя', middle_name='отчество')
QUESTIONS = dict(last_name='Назовите фамилию пациента. Можно произнести её по буквам.',
                 first_name='Назовите имя пациента.', middle_name='Назовите отчество пациента. Если отчества нет, скажите: нет отчества.',
                 full_name='Назовите фамилию, имя и отчество пациента.',
                 birth_date='Назовите дату рождения пациента: день, месяц и полный год.',
                 phone='Назовите номер телефона пациента, указанный в картотеке.',
                 oms='Найдено несколько совпадений. Назовите номер полиса ОМС или его последние четыре цифры.')


def folded(value):
    return ' '.join(unicodedata.normalize('NFC',str(value or '')).casefold().replace('ё', 'е').split())


def phone_digits(value):
    digits=re.sub(r'\D', '', str(value or ''))
    if len(digits)==11 and digits[0]=='8':digits='7'+digits[1:]
    return digits if 7<=len(digits)<=15 else ''


def near_phone_owner(parts,row):
    """One ASR character error anywhere in FIO; е/ё are equivalent.

    Never use a similarity ranking across the whole patient database. Caller
    phone and full birth date must first select exactly one owner.
    """
    words=folded(row['full_name']).split()
    if len(words)!=3:return False
    edits=0
    for key,b in zip(NAME_FIELDS,words):
        a=folded(parts.get(key))
        if a==b:continue
        if min(len(a),len(b))<3 or abs(len(a)-len(b))>1:return False
        if len(a)==len(b):
            differences=[i for i,(x,y) in enumerate(zip(a,b)) if x!=y]
            # Adjacent transposition also counts as one recognition error.
            one=len(differences)==1 or (len(differences)==2 and differences[1]==differences[0]+1 and a[differences[0]]==b[differences[1]] and a[differences[1]]==b[differences[0]])
        else:
            if len(a)>len(b):a,b=b,a
            one=any(a==b[:i]+b[i+1:] for i in range(len(b)))
        if not one:return False
        edits+=1
    return edits<=1


def foreign_language_request(text):
    """A declared language barrier, not the presence of a foreign surname."""
    return bool(re.search(r"\b(?:i\s+(?:do\s+not|don['’]?t)\s+(?:speak|understand)\s+russian|"
                          r"i\s+(?:only\s+)?speak\s+english|не\s+(?:говорю|понимаю)\s+по[ -]?русски)\b",text,re.I))


def spelled_name(text):
    text=re.sub(r'^(?:по буквам|фамилия по буквам|имя по буквам|отчество по буквам)[: ,]*','',text.strip(),flags=re.I)
    letters=re.split(r'[\s,.-]+',text.strip(' .,!'))
    if len(letters)>=2 and all(re.fullmatch('[А-Яа-яЁёA-Za-z]',letter) for letter in letters):
        return ''.join(letters).title()
    return None


def spoken_letters(text):
    """Check a model's reconstruction against explicit Russian letter names."""
    aliases = dict(zip(
        'а бэ вэ гэ дэ е ё жэ зэ и й ка эль эм эн о пэ эр эс тэ у эф ха цэ чэ ша ща э ю я'.split(),
        'абвгдеёжзийклмнопрстуфхцчшщэюя'))
    aliases.update({'мягкий знак': 'ь', 'твердый знак': 'ъ', 'твёрдый знак': 'ъ', 'и краткое': 'й', 'ы': 'ы'})
    letters = []
    for part in re.split(r'[,;\n]+|\s+-\s+', text.strip(' .!')):
        part = part.strip().casefold()
        if not part:
            continue
        if part in aliases:
            letters.append(aliases[part])
        elif re.fullmatch('[а-яё]', part):
            letters.append(part)
        else:
            return spelled_name(text)
    return ''.join(letters).title() if len(letters) >= 2 else None


def owner_literal_parts(text, owners):
    """Use phone owners only to label actually spoken tokens; never fill a gap."""
    candidates = []
    for owner in owners:
        parts = owner.split()
        if len(parts) != 3:
            continue
        found = {}
        for key, part in zip(NAME_FIELDS, parts):
            match = re.search(r'(?<!\w)' + re.escape(part) + r'(?!\w)', text, re.I)
            if match:
                found[key] = match[0]
        if found:
            candidates.append(found)
    if not candidates:
        return {}
    best = max(len(value) for value in candidates)
    winners = [value for value in candidates if len(value) == best]
    if not all(value == winners[0] for value in winners):
        return {}
    # A known surname must not hide a different, explicitly supplied first name.
    remaining = text
    for part in winners[0].values():
        remaining = re.sub(r'(?<!\w)' + re.escape(part) + r'(?!\w)', '', remaining, flags=re.I)
    return winners[0] if not remaining.strip(' ,.;') else {}


def clean_name(value):
    if not isinstance(value,str):return None
    value=' '.join(value.strip(' .,!').split())
    return value if re.fullmatch(r"[^\W\d_]+(?:[-'’ ][^\W\d_]+){0,3}",value,re.UNICODE) else None


def literal_parts(value,text):
    """Reject invented/transliterated/reordered characters from a generative model."""
    if not isinstance(value,dict) or set(value)-set(NAME_FIELDS):return {}
    source=' '.join(unicodedata.normalize('NFC',str(text)).casefold().split())
    result={}
    for key,part in value.items():
        if part in (None,''):continue
        part=clean_name(part)
        if not part or not re.search(r'(?<!\w)'+re.escape(' '.join(unicodedata.normalize('NFC',part).casefold().split()))+r'(?!\w)',source):return {}
        result[key]=part
    # No source token may fill two different fields.
    if len({folded(v) for v in result.values()})!=len(result):return {}
    return result


def surname_like(value):
    return folded(value).endswith(('ов','ова','ев','ева','ин','ина','ский','ская','цкий','цкая'))


def split_birth_utterance(text, *, today=None):
    """Extract a complete explicit date without moving/guessing any name letters.

    The birth parser requires a complete calendar date with a four digit year.
    A malformed explicit DOB is excluded from the name span, never guessed.
    Without a label, only a parsable date suffix is removed from a name span.
    """
    from .identity import birth_date_input
    marker = re.search(r'\b(?:дата\s+рождения|(?:я\s+)?родил(?:ся|ась))\b[\s,:]*', text, re.I)
    if marker:
        result = birth_date_input(text[marker.end():], today=today)
        return text[:marker.start()].strip(' ,.;'), result.get('value') if result['status']=='valid' else None
    # Dates can arrive as "Иванов Иван 12.04.1985" or the actual ASR words.
    starts = [match.start() for match in re.finditer(r'(?<!\S)\S+', text)]
    if len(starts) > 60:
        return text, None
    for start in starts:
        candidate = text[start:].strip(' ,.;')
        result = birth_date_input(candidate, today=today)
        if result['status']=='valid':
            return text[:start].strip(' ,.;'), result['value']
    return text, None


class DialogueModel:
    """Small compatible-API boundary, independent of the slot selection agent."""
    def __init__(self,settings):
        self.settings=settings
        self.questions={}

    def complete(self,task,schema):
        if any(os.environ.get(key, '').lower() in {'1', 'true', 'yes'} for key in ('CI', 'GITHUB_ACTIONS', 'ACT', 'CLINIC_ACCEPTANCE_TEST')):
            return None
        item=self.settings.read().get('dialogue', {'enabled': False})
        if not item['enabled']:return None
        from .module_settings import secret
        local=item['provider']=='local'
        base=os.environ.get('DIALOGUE_LOCAL_URL','http://127.0.0.1:11435/v1') if local else ('https://openrouter.ai/api/v1' if item['provider']=='openrouter' else item['base_url'])
        headers={'Content-Type':'application/json'}
        if not local:headers['Authorization']='Bearer '+secret(item)
        payload={'model':item['model'],'temperature':0,'max_tokens':240,'stream':False,
                 'messages':[{'role':'system','content':'Ты помощник регистратуры. Только JSON по схеме. Не придумывай данные, не подтверждай личность, не выполняй запись. /no_think'},
                             {'role':'user','content':json.dumps(task,ensure_ascii=False)}],
                 'response_format':{'type':'json_schema','json_schema':{'name':'identity_form','strict':True,'schema':schema}}}
        if local:
            payload['reasoning_effort'] = 'none'
        req=urllib.request.Request(base.rstrip('/')+'/chat/completions',data=json.dumps(payload).encode(),headers=headers,method='POST')
        try:
            with open_http(req,timeout=8) as response:
                raw=response.read(32769)
            if len(raw)>32768:raise ValueError('model_response_too_large')
            return json.loads(json.loads(raw)['choices'][0]['message']['content'])
        except (OSError,ValueError,KeyError,IndexError,TypeError):return None

    def extract(self,text,requested):
        schema={'type':'object','properties':{key:{'type':'string'} for key in NAME_FIELDS},'required':list(NAME_FIELDS),'additionalProperties':False}
        value=self.complete({'task':'Выдели только явно произнесённые части имени пациента. Недостающие поля — пустая строка. Не исправляй орфографию. Учитывай вопрос. Иван Сергеевич: имя Иван, отчество Сергеевич, фамилия неизвестна.','requested_field':requested,'utterance':text},schema)
        return literal_parts(value,text), bool(value)

    def extract_spelling(self, text, requested, owners=()):
        # Constrain the small model to whole source tokens and independently
        # decoded letter sequences. It cannot emit a substring such as «им»
        # from «Ким», or treat a single phonetic letter name as the surname.
        masked = text
        spelled = {}
        for key, label in [('last_name', 'фамилия'), ('first_name', 'имя'), ('middle_name', 'отчество')]:
            match = re.search(r'\b' + label + r'\s+по\s+буквам\s*[: ,]*(.*?)(?=[.;]|$)', text, re.I)
            if match:
                chunk = match[1].strip(' ,.')
                decoded = spoken_letters(chunk)
                if decoded:
                    spelled[key] = (decoded, chunk)
                    masked = masked.replace(match[0], ' ')
        stop = {'фамилия','имя','отчество','по','буквам','моя','меня','зовут','это','через','буква'}
        literal = {token for token in re.findall(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", masked)
                   if token.casefold() not in stop and len(token) > 1}
        fields = {key: {'type': 'string', 'enum': ['', spelled[key][0]] if key in spelled else ['', *sorted(literal)]}
                  for key in NAME_FIELDS}
        evidence_fields = {key: {'type': 'string', 'enum': ['', spelled[key][1]] if key in spelled else ['', *sorted(literal)]}
                           for key in NAME_FIELDS}
        schema = {'type': 'object', 'properties': {
            'parts': {'type': 'object', 'properties': fields, 'required': list(NAME_FIELDS), 'additionalProperties': False},
            'evidence': {'type': 'object', 'properties': evidence_fields, 'required': list(NAME_FIELDS), 'additionalProperties': False}},
            'required': ['parts', 'evidence'], 'additionalProperties': False}
        answer = self.complete({'task': 'Разбери диктовку ФИО по буквам. Названия букв объединяй в слово: '
                                'например, фамилия «бэ, а, ка» = Бак, evidence="бэ, а, ка". '
                                'Не считай название отдельной буквы фамилией. Сохраняй первую букву имени. '
                                'Только явно произнесенные части. '
                                'Для каждой части evidence — дословный непрерывный фрагмент с буквами или самим именем. '
                                'Недостающее — пустая строка. ФИО владельцев номера — только подсказка, не дополняй из них пропуски.',
                                'utterance': text, 'requested_field': requested, 'phone_owner_names': list(owners),
                                'decoded_spelling': {key: {'value': value, 'evidence': evidence} for key, (value, evidence) in spelled.items()}}, schema)
        if not isinstance(answer, dict) or set(answer) != {'parts', 'evidence'}:
            return {}
        if not all(isinstance(answer[key], dict) and set(answer[key]) == set(NAME_FIELDS) for key in answer):
            return {}
        found = {}
        for key, value in answer['parts'].items():
            if not value:
                continue
            evidence = answer['evidence'][key]
            if value not in fields[key]['enum'] or evidence not in evidence_fields[key]['enum']:
                return {}
            if not clean_name(value) or not isinstance(evidence, str) or not evidence or not re.search(r'(?<!\w)' + re.escape(evidence) + r'(?!\w)', text, re.I):
                return {}
            label = {'last_name':'фамилия', 'first_name':'имя', 'middle_name':'отчество'}[key]
            spelling = re.search(r'\b' + label + r'\s+по\s+буквам\s*[: ,]*(.*?)(?=[.;]|$)', text, re.I)
            if spelling and spoken_letters(spelling[1]) != value:
                return {}
            literal = literal_parts({key: value}, evidence)
            spelled = spoken_letters(evidence)
            if not literal and (not spelled or spelled.casefold() != value.casefold()):
                return {}
            found[key] = value
        return found

    def question(self,requested):
        # Contains field labels only: no patient data is sent to phrase a question.
        item=self.settings.read().get('dialogue', {'enabled': False})
        cache=(json.dumps(item,sort_keys=True),requested)
        if cache in self.questions:return self.questions[cache]
        schema={'type':'object','properties':{'field':{'type':'string','enum':[requested]},'question':{'type':'string'}},'required':['field','question'],'additionalProperties':False}
        value=self.complete({'task':'Сформулируй один короткий вежливый вопрос о недостающем поле пациента. Не проси другие поля, не приводи примеров имён, не сообщай результат поиска.','field':requested,'reference':QUESTIONS[requested]},schema)
        text=value.get('question','') if isinstance(value,dict) else ''
        root={'last_name':'фамил','first_name':'имя','middle_name':'отчеств','birth_date':'рожден'}.get(requested)
        if value and value.get('field')==requested and root and root in folded(text) and 5<len(text)<220 and not re.search(r'\d|записан|найден|иванов|петров',folded(text)):
            self.questions[cache]=text
            return text
        return QUESTIONS[requested]


@dataclass
class IdentityForm:
    parts: dict = field(default_factory=dict)
    birth_date: str = ''
    no_middle_name: bool = False
    pending: str = ''
    requested: str = 'full_name'
    confirmed_readback: bool = False

    @property
    def full_name(self):
        return ' '.join(self.parts.get(key,'') for key in NAME_FIELDS).strip()

    @property
    def missing(self):
        return [key for key in NAME_FIELDS if not self.parts.get(key) and not (key=='middle_name' and self.no_middle_name)]

    def reset(self):
        self.parts.clear();self.birth_date='';self.pending='';self.requested='full_name';self.confirmed_readback=False;self.no_middle_name=False

    def next_field(self):
        missing=self.missing
        self.requested=('full_name' if len(missing)==3 else missing[0]) if missing else 'birth_date'
        return self.requested

    def accept_name(self,text,model=None,*,today=None,phone_owner_names=()):
        # Collect independent literal fields before asking only what is missing.
        name_text, parsed_birth = split_birth_utterance(text, today=today)
        changed_birth = bool(parsed_birth and parsed_birth != self.birth_date)
        if changed_birth:
            self.birth_date = parsed_birth
            self.confirmed_readback = False
        text = name_text
        low=folded(text).strip(' .!')
        if self.pending:
            kind=next((key for key,root in [('last_name','фамил'),('first_name','имя'),('middle_name','отчеств')] if root in low),None)
            if kind:
                self.parts[kind]=self.pending;self.pending='';self.confirmed_readback=False
                if kind=='middle_name':self.no_middle_name=False
                self.next_field();return True
        if low in {'нет отчества','отчества нет','без отчества','у меня нет отчества'}:
            self.no_middle_name=True;self.parts['middle_name']='';self.confirmed_readback=False;self.next_field();return True
        requested=self.requested
        spelled=spelled_name(text)
        spelling_request = bool(spelled or re.search(r'\b(?:по\s+буквам|букв[аыу]|через\s+[её])\b', text, re.I))
        if model and spelling_request:
            extracted = model.extract_spelling(text, requested, phone_owner_names)
            if extracted:
                self.parts.update(extracted);self.pending='';self.confirmed_readback=False
                self.next_field();return True
        if spelled:
            if requested in NAME_FIELDS:self.parts[requested]=spelled
            else:self.pending=spelled
            self.confirmed_readback=False
            if requested=='middle_name':self.no_middle_name=False
            self.next_field();return True
        value=re.sub(r'^(?:меня зовут|моя фамилия|моё имя|мое имя|фамилия|имя|отчество|это|фио)[: ,]*','',text.strip(),flags=re.I).strip(' .!')
        words=value.split()
        found=owner_literal_parts(value, phone_owner_names)
        if not found and '?' not in text and clean_name(value):
            patronym=lambda w:folded(w).endswith(('вич','вна','ична','оглы','кызы'))
            if len(words)==3:
                # Both common orders. For other name structures ask the model.
                if patronym(words[1]):found=dict(zip(NAME_FIELDS,[words[2],words[0],words[1]]))
                elif patronym(words[2]):found=dict(zip(NAME_FIELDS,words))
            elif len(words)==2 and patronym(words[1]):found={'first_name':words[0],'middle_name':words[1]}
            elif len(words)==2 and surname_like(words[0]):found={'last_name':words[0],'first_name':words[1]}
            elif len(words)==2 and surname_like(words[1]):found={'first_name':words[0],'last_name':words[1]}
            elif len(words)==1 and requested in NAME_FIELDS:found={requested:value}
            elif len(words)==1 and patronym(value):found={'middle_name':value}
            elif len(words)==1 and folded(value).endswith(('ов','ова','ев','ева','ин','ина','ский','ская','цкий','цкая')):found={'last_name':value}
        if not found and model and text.strip() and not spelling_request:
            found,_=model.extract(text,requested)
        if found:
            self.parts.update(found);self.pending='';self.confirmed_readback=False
            if found.get('middle_name'):self.no_middle_name=False
            self.next_field();return True
        if len(words)==1 and clean_name(value) and folded(value) not in {'да','нет','повторите','продолжить','здравствуйте'}:
            self.pending=value;return True
        return changed_birth

    def question(self,model=None):
        if self.pending:return 'Вы назвали «'+self.pending+'». Это имя или фамилия пациента?'
        requested=self.next_field()
        return QUESTIONS[requested]

    def readback(self):
        return identity_readback(self.full_name,self.birth_date,self.no_middle_name)


def identity_readback(full_name,birth_date,no_middle_name=False):
    """Read the original complete spelling, including compound database surnames."""
    from calls.tts.verbalize import date_to_words
    from datetime import date
    return 'Правильно ли я понял: '+full_name+(' (отчества нет)' if no_middle_name else '')+', дата рождения '+date_to_words(date.fromisoformat(birth_date), with_year=True, with_weekday=False)+'? Да — один. Исправить — ноль. Можно назвать фамилию по буквам.'


def candidate_query(query,call_id,parts,birth=None,phone=None,policy=None,cohort=None):
    """Shared SQL contract over a view supplied by either clinic data adapter."""
    return query(call_id,'''SELECT id,full_name,birth_date,district,phone FROM identity_people
WHERE (? IS NULL OR clinic_name(last_name)=clinic_name(?))
AND (? IS NULL OR clinic_name(first_name)=clinic_name(?))
AND (? IS NULL OR clinic_name(middle_name)=clinic_name(?))
AND (? IS NULL OR birth_date=?)
AND (? IS NULL OR clinic_phone(phone)=?)
AND (? IS NULL OR (length(?)=16 AND oms=?) OR (length(?) BETWEEN 4 AND 15 AND substr(oms,-length(?))=?))
AND (? IS NULL OR id IN (SELECT value FROM json_each(?))) ORDER BY id LIMIT 101''',
        tuple(v for key in NAME_FIELDS for v in (parts.get(key),parts.get(key)))+
        (birth,birth,phone,phone,policy,policy,policy,policy,policy,policy,
         json.dumps(cohort) if cohort is not None else None,json.dumps(cohort) if cohort is not None else None))

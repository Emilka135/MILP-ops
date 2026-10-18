"""Explicit Russian speech text. Independent of the acoustic model's tokenizer."""
from datetime import date, time
import re
from .verbalize import date_to_words, time_to_words, number_to_words, digits_to_words, phone_to_words

NORMALIZER_VERSION = 'ru-clinic-4'
ABBREVIATIONS = {
    'смс': 'эс эм эс', 'sms': 'эс эм эс', 'фио': 'фамилия имя и отчество',
    'омс': 'обязательное медицинское страхование', 'дмс': 'добровольное медицинское страхование',
    'снилс': 'снилс',
    'фгдс': 'фиброгастродуоденоскопия', 'эгдс': 'эзофагогастродуоденоскопия',
    'узи': 'ультразвуковое исследование', 'экг': 'электрокардиограмма',
    'мрт': 'магнитно резонансная томография', 'кт': 'компьютерная томография',
}
LETTERS = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
    ['а','бэ','вэ','гэ','дэ','е','ё','жэ','зэ','и','и краткое','ка','эль','эм','эн','о','пэ','эр','эс','тэ','у','эф','ха','цэ','че','ша','ща','твёрдый знак','ы','мягкий знак','э','ю','я']))


def spoken_phone(raw):
    digits=''.join(c for c in raw if c.isdigit())
    if len(digits)!=11:raise ValueError('Номер телефона должен содержать 11 цифр')
    def group(value):
        return digits_to_words(value) if len(value)>1 and value[0]=='0' else number_to_words(int(value))
    return ', '.join(group(v) for v in [digits[0],digits[1:4],digits[4:7],digits[7:9],digits[9:11]])


def year_words(year):
    if year % 100 == 0:
        # Rare boundaries must be explicit rather than silently read as a cardinal year.
        if year == 2000:return 'двухтысячного'
        raise ValueError('Укажите год словами: '+str(year))
    n=year%100;ordinals={1:'первого',2:'второго',3:'третьего',4:'четвёртого',5:'пятого',6:'шестого',7:'седьмого',8:'восьмого',9:'девятого',10:'десятого',11:'одиннадцатого',12:'двенадцатого',13:'тринадцатого',14:'четырнадцатого',15:'пятнадцатого',16:'шестнадцатого',17:'семнадцатого',18:'восемнадцатого',19:'девятнадцатого',20:'двадцатого',30:'тридцатого',40:'сорокового',50:'пятидесятого',60:'шестидесятого',70:'семидесятого',80:'восьмидесятого',90:'девяностого'}
    tail=ordinals.get(n) or number_to_words(n//10*10)+' '+ordinals[n%10]
    return number_to_words(year-n)+' '+tail


def spoken_date(year,month,day,with_year=True):
    value=date(int(year),int(month),int(day))
    return date_to_words(value,with_weekday=False)+((' '+year_words(value.year)+' года') if with_year else '')


def normalize_text(text):
    text=str(text).replace('\u00a0',' ').replace('\u202f',' ')
    if '{' in text or '}' in text:raise ValueError('Не подставлены данные в речевой шаблон')
    # Phones precede numeric dates; long identifiers are always read digit by digit.
    text=re.sub(r'(?<!\w)(?:\+?[78][\s(]*\d{3}[)\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2})(?!\d)',lambda m:spoken_phone(m[0]),text)
    text=re.sub(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)',lambda m:spoken_date(*m.groups()),text)
    text=re.sub(r'(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)',lambda m:spoken_date(m[3],m[2],m[1]),text)
    # ASR often returns dates with a written month; speak these consistently too.
    months='января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
    month_pattern='|'.join(months)
    text=re.sub(r'(?<!\d)(\d{1,2})\s+('+month_pattern+r')(?:\s+(\d{4})\s*(?:года|г\.)?)?',
        lambda m:spoken_date(m[3] or '2000',months.index(m[2].lower())+1,m[1],bool(m[3])),text,flags=re.I)
    # DD.MM without a year is also a decimal. Only appointment/date context
    # authorises reading it as a date; an isolated ambiguous value is rejected.
    def short_date(match):
        day, month = int(match[1]), int(match[2])
        before = text[:match.start()]
        after = text[match.end():]
        date_context = re.search(r'(?:при[её]м(?:а|у|е)?|запис[ьи]|записаны|дата|дату|исследование|исследования)\s*:?\s*(?:на\s+)?$', before, re.I)
        date_context = date_context or re.match(r'\s+(?:в\s+\d{1,2}:|года\b)', after, re.I)
        if date_context:
            return spoken_date(2000, month, day, False)
        if 1 <= day <= 31 and 1 <= month <= 12:
            raise ValueError('Неоднозначное число или дата: '+match[0]+'. Укажите дату с годом, а дробное число через запятую')
        return match[0]
    text=re.sub(r'(?<![\w.])(\d{1,2})\.(\d{2})(?!\d|\.\d)',short_date,text)
    text=re.sub(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)',lambda m:time_to_words(time(int(m[1]),int(m[2]))),text)
    # Signs must be spoken. Date/telephone separators have already been consumed.
    text=re.sub(r'(?<![\w\d])[-−](?=\d)', 'минус ', text)
    text=re.sub(r'(?<![\w\d])\+(?=\d)', 'плюс ', text)
    for abbreviation,words in ABBREVIATIONS.items():
        text=re.sub(r'(?<!\w)'+re.escape(abbreviation)+r'(?!\w)',words,text,flags=re.I)
    text=re.sub(r'(?<!\w)([А-ЯЁ]{2,5})(?!\w)',lambda m:' '.join(LETTERS[c.lower()] for c in m[1]),text)
    text=text.replace('№',' номер ').replace('%',' процентов ')
    text=re.sub(r'(?<!\w)(\d+)[,.](\d+)(?!\w)',lambda m:number_to_words(int(m[1]))+' запятая '+digits_to_words(m[2]),text)
    text=re.sub(r'\d+',lambda m:digits_to_words(m[0]) if len(m[0])>=7 or (len(m[0])>1 and m[0][0]=='0') else number_to_words(int(m[0])),text)
    if re.search(r'\d|[a-zA-Z]',text):raise ValueError('Укажите иностранное имя или сокращение русскими буквами для произношения')
    return re.sub(r'\s+',' ',text).strip()

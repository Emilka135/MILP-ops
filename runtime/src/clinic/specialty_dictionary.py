"""Auditable colloquial names of specialties; never a symptom-to-diagnosis table."""
import hashlib,json,os,re,threading
from pathlib import Path


def normalized(text):return re.sub(r'\s+',' ',re.sub(r'[^а-яёa-z0-9\s-]',' ',text.casefold().replace('ё','е'))).strip()


class SpecialtyDictionary:
    def __init__(self,path=None):
        configured=Path(os.environ.get('MODULE_SETTINGS_FILE','/module-config/modules.json')).with_name('specialties.json')
        default=configured if configured.exists() else Path(__file__).with_name('specialty_names.json')
        self.path=Path(path or os.environ.get('SPECIALTY_DICTIONARY_FILE',str(default)))
        self.lock=threading.RLock()
    def read(self):
        raw=self.path.read_text()
        return {**json.loads(raw),'revision':hashlib.sha256(raw.encode()).hexdigest()}
    def save(self,items,revision):
        with self.lock:
            current=self.read()
            if current['revision']!=revision:raise ValueError('Словарь изменён другим сотрудником. Обновите страницу.')
            if not isinstance(items,list) or not 1<=len(items)<=200:raise ValueError('Укажите от 1 до 200 специальностей')
            ids=set()
            aliases={}
            for item in items:
                if not isinstance(item,dict) or set(item)-{'id','label','aliases','referral_required'} or not isinstance(item.get('aliases'),list) or not item['aliases']:raise ValueError('Укажите названия и синонимы')
                if not re.fullmatch(r'[a-z][a-z0-9_]{1,59}',item.get('id','')) or item['id'] in ids:raise ValueError('Нужен уникальный идентификатор специальности')
                ids.add(item['id'])
                if not isinstance(item.get('label'),str) or not 1<len(item['label'])<100:raise ValueError('Укажите название специальности')
                if item.get('referral_required') is not None and type(item['referral_required']) is not bool:raise ValueError('Укажите необходимость направления')
                item['referral_required']=item.get('referral_required')
                for alias in item['aliases']:
                    if not isinstance(alias,str) or not 1<len(alias)<150:raise ValueError('Некорректный синоним')
                    key=normalized(alias)
                    if key in aliases and aliases[key]!=item['id']:raise ValueError('Одинаковый синоним у разных врачей: '+alias)
                    aliases[key]=item['id']
            raw=json.dumps({'specialties':items},ensure_ascii=False,indent=2)
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.path.with_suffix('.tmp');temp.write_text(raw);temp.replace(self.path)
            return self.read()
    def resolve(self,text):
        low=normalized(text)
        # A correction's last clause supersedes a negated previous specialty.
        clauses=re.split(r'\b(?:а|но|вместо)\b',low)
        hits={};excluded=set()
        for item in self.read()['specialties']:
            for alias in item['aliases']:
                key=normalized(alias)
                for clause in clauses:
                    match=re.search(r'(?<!\w)'+re.escape(key)+r'(?!\w)',clause)
                    if match:
                        if re.search(r'\b(?:не|без)\s+(?:к\s+)?$',clause[:match.start()]):excluded.add(item['id'])
                        else:hits[item['id']]=item['label']
        hits={k:v for k,v in hits.items() if k not in excluded}
        if len(hits)==1:return {'status':'resolved','id':next(iter(hits)),'label':next(iter(hits.values())),'source':'dictionary','excluded':list(excluded)}
        return {'status':'ambiguous' if hits else 'missing','candidates':list(hits),'excluded':list(excluded)}

    def policy(self, specialty):
        key=normalized(specialty)
        for item in self.read()['specialties']:
            if key in {normalized(item['id']),normalized(item['label']),*(normalized(a) for a in item['aliases'])}:
                return item
        return None

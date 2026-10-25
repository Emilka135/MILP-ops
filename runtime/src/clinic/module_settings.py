"""Persisted module configuration. Secrets are local references, never browser data."""
import copy
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    'tts': {'enabled':True,'provider':'local','model':'v5_cis_base','voice':'ru_ekaterina','base_url':'','key_env':'OPENROUTER_API_KEY'},
    'asr': {'enabled':True,'provider':'local','model':'gigaam-v3-e2e-rnnt','voice':'','base_url':'','key_env':'OPENROUTER_API_KEY'},
    'nlu': {'enabled':True,'provider':'local','model':'e5','voice':'','base_url':'','key_env':'OPENROUTER_API_KEY'},
    'agent': {'enabled':True,'provider':'local','model':'deterministic','voice':'','base_url':'','key_env':'OPENROUTER_API_KEY'},
    'dialogue': {'enabled':False,'provider':'local','model':'qwen3:1.7b','voice':'','base_url':'','key_env':'OPENROUTER_API_KEY'},
    'crm': {'enabled':True,'provider':'local','model':'sqlite / МИС','voice':'','base_url':'','key_env':''},
    'sms': {'enabled':True,'provider':'local','model':'gateway','voice':'','base_url':'','key_env':''},
    'operator': {'enabled':True,'provider':'local','model':'queue','voice':'','base_url':'','key_env':''},
    'client_tags': {'enabled':False,'provider':'local','model':'call-scoped','voice':'','base_url':'','key_env':''},
    'preferences': {'enabled':False,'provider':'local','model':'explicit-only','voice':'','base_url':'','key_env':''},
    'scenario_extensions': {'enabled':False,'provider':'local','model':'reviewed-build','voice':'','base_url':'','key_env':''},
}
LABELS = dict(tts='Озвучка',asr='Распознавание речи',nlu='Понимание речи',agent='Подбор слотов',dialogue='Сложные уточнения ФИО и времени (LLM)',crm='Картотека и запись',sms='Уведомления',operator='Передача сотруднику',client_tags='Метки клиентов',preferences='Предпочтения',scenario_extensions='Расширения сценариев')


class ModuleSettings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            stored = json.loads(self.path.read_text()) if self.path.exists() else {}
            if stored.get('nlu',{}).get('provider','local')=='local' and stored.get('nlu',{}).get('model')=='hash':stored['nlu']['model']='e5'
            return {key:{**copy.deepcopy(value),**stored.get(key,{})} for key,value in DEFAULTS.items()}

    def public(self):
        return {'modules':self.read(),'defaults':copy.deepcopy(DEFAULTS),'labels':LABELS,'providers':['local','openrouter','compatible'],
                'scope':'Настройки применяются к новым операциям. Снимок правил тестового звонка сохраняется в журнале.',
                'secrets':'Ключ: переменная окружения или файл /run/secrets/<имя_переменной_в_нижнем_регистре>.key'}

    def save(self, values):
        if not isinstance(values,dict) or set(values) != set(DEFAULTS):
            raise ValueError('Передайте настройки всех известных модулей')
        checked = {}
        for key, value in values.items():
            if not isinstance(value,dict) or set(value)-set(DEFAULTS[key]): raise ValueError('Неизвестное поле модуля')
            item = {**DEFAULTS[key],**value}
            if type(item['enabled']) is not bool: raise ValueError('enabled должен быть логическим')
            if item['provider'] not in {'local','openrouter','compatible'}: raise ValueError('Неизвестный провайдер')
            if key not in {'tts','asr','nlu','agent','dialogue'} and item['provider']!='local': raise ValueError('Этот модуль использует адаптер приложения, а не речевую модель')
            for field in ('model','voice','base_url','key_env'):
                if not isinstance(item[field],str) or len(item[field])>300: raise ValueError('Некорректное поле '+field)
            if not item['model'].strip(): raise ValueError('Укажите модель или стек')
            if item['provider']=='local' and key in {'tts','asr','nlu','agent','dialogue'}:
                choices = {'tts':{'v5_cis_base'},'asr':{'gigaam-v3-e2e-rnnt'},'nlu':{'e5'},'agent':{'deterministic'},'dialogue':{'qwen3:1.7b'}}
                if key!='dialogue' and item['model'] not in choices[key]: raise ValueError('Локальная модель не установлена / не разрешена')
            if item['provider']!='local':
                if not re.fullmatch(r'[A-Z][A-Z0-9_]{2,79}',item['key_env']): raise ValueError('Укажите имя переменной ключа')
            if item['provider']=='compatible' or key=='dialogue' and item['base_url']:
                    url=urlsplit(item['base_url'])
                    if url.scheme not in {'http','https'} or not url.hostname or url.username or url.password or url.query or url.fragment: raise ValueError('Нужен базовый HTTP(S) URL без секрета')
                    if url.scheme=='http' and item['provider']!='local' and url.hostname not in {'localhost','127.0.0.1','host.docker.internal'}: raise ValueError('Для удалённого сервиса нужен HTTPS')
            checked[key]=item
        with self.lock:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(checked,ensure_ascii=False,indent=2))
            temp.replace(self.path)
        return self.public()


def configured():
    return ModuleSettings(os.environ.get('MODULE_SETTINGS_FILE',os.environ.get('EDITOR_DATA','var/editor')+'/modules.json'))


def secret(item):
    key = os.environ.get(item['key_env'],'').strip()
    path = Path(os.environ.get('MODULE_SECRETS_DIR','secrets')) / (item['key_env'].lower()+'.key')
    # Compatibility with the existing project secret files.
    legacy = {'OPENROUTER_API_KEY':'openrouter.key','OPENAI_API_KEY':'openai.key'}.get(item['key_env'])
    if not key and path.is_file(): key=path.read_text().strip()
    if not key and legacy:
        path=path.parent/legacy
        if path.is_file(): key=path.read_text().strip()
    if not key: raise ValueError('Не настроен локальный ключ '+item['key_env'])
    return key


def model_catalog(module):
    """Public provider metadata, filtered by the module's actual modality."""
    if module not in {'tts','asr','nlu','agent','dialogue'}:raise ValueError('У модуля нет каталога моделей')
    from calls.services.netutil import open_http
    import urllib.request
    try:
        with open_http(urllib.request.Request('https://openrouter.ai/api/v1/models'),timeout=10) as response:
            raw=response.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:raise ValueError('Слишком большой каталог')
        models=json.loads(raw)['data']
    except (OSError,KeyError,TypeError,ValueError):raise ValueError('Каталог OpenRouter недоступен; можно ввести ID модели вручную') from None
    items=[]
    for model in models:
        arch=model.get('architecture',{})
        inputs,outputs=arch.get('input_modalities',[]),arch.get('output_modalities',[])
        suitable=(bool(set(outputs)&{'audio','speech'}) if module=='tts' else ('audio' in inputs and 'text' in outputs) if module=='asr' else 'text' in inputs and 'text' in outputs)
        if suitable:items.append({'id':model['id'],'name':model.get('name',model['id'])})
    return {'models':items,'source':'https://openrouter.ai/api/v1/models','note':'Доступ и голос зависят от провайдера и ключа; каталог не является проверкой вызова.'}

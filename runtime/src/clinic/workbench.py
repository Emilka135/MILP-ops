"""UI-independent, configured MIS/SQL adapters and bundled reference services."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from urllib.parse import urlencode, urlsplit

from calls.services.netutil import open_http
from .mis_contract import lookup_http
from .module_settings import secret

DEFAULT_FILE=Path(__file__).resolve().parents[3]/'scenarios/mis/connections.defaults.json'
LIMIT=2*1024*1024


class Connections:
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.Lock()
    def read(self):
        value=json.loads((self.path if self.path.exists() else DEFAULT_FILE).read_text())
        revision=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
        return {**value,'revision':revision,'defaults':json.loads(DEFAULT_FILE.read_text())}
    def save(self,values,revision):
        if not isinstance(values,dict) or set(values)!={'mis','database'}:raise ValueError('Укажите подключения МИС и БД')
        checked=deepcopy(values)
        for kind,item in checked.items():
            if not isinstance(item,dict) or set(item)!={'adapter','label','base_url','key_env'}:raise ValueError('Некорректное подключение')
            if any(not isinstance(v,str) or len(v)>2048 for v in item.values()):raise ValueError('Некорректное поле подключения')
            if not item['label'].strip():raise ValueError('Укажите название подключения')
            if item['adapter'] not in ({'reference_mis','http_mis'} if kind=='mis' else {'reference_sqlite','http_sql'}):raise ValueError('Неизвестный адаптер')
            if item['adapter'].startswith('reference_'):
                item['base_url']=json.loads(DEFAULT_FILE.read_text())[kind]['base_url'];item['key_env']=''
            else:
                u=urlsplit(item['base_url'])
                if u.scheme not in {'http','https'} or not u.hostname or u.username or u.password or u.query or u.fragment:raise ValueError('Нужен HTTP(S) URL без секрета')
                if u.hostname=='metadata.google.internal' or u.hostname.startswith('169.254.'):raise ValueError('Служебный адрес запрещён')
                if item['key_env'] and not re.fullmatch('[A-Z][A-Z0-9_]{2,79}',item['key_env']):raise ValueError('Укажите имя переменной ключа')
        with self.lock:
            if revision!=self.read()['revision']:raise ValueError('Подключения изменены. Обновите настройки.')
            self.path.parent.mkdir(parents=True,exist_ok=True);temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(checked,ensure_ascii=False,indent=2));temp.replace(self.path)
        return self.read()


def connection_config():
    from .module_settings import configured
    return Connections(configured().path.with_name('connections.json'))


def http_json(item,path='',*,method='POST',body=None,query=None):
    """Exactly one bounded request: a lost write response must never be retried."""
    url=item['base_url'].rstrip('/')+path
    if query:url+='?'+urlencode(query)
    headers={'Content-Type':'application/json','Accept':'application/json'}
    if item.get('key_env'):headers['Authorization']='Bearer '+secret(item)
    req=urllib.request.Request(url,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers=headers,method=method)
    try:
        with open_http(req,timeout=8) as response:raw=response.read(LIMIT+1)
        if len(raw)>LIMIT:raise ValueError('Ответ сервиса превышает 2 MiB')
        return json.loads(raw)
    except (OSError,ValueError):raise ValueError('Сервис недоступен или вернул неверный ответ. Результат изменения неизвестен; повтор автоматически не выполняется.') from None


class ReferenceSql:
    """Real SQLite, bounded statements and explicit employee write consent."""
    TABLES={'patients','identity_people','doctors','district_doctors','slots','bookings','referrals'}
    MUTABLE={'patients','doctors','district_doctors','slots','bookings','referrals'}
    def __init__(self,store):self.store=store
    def execute(self,sql,params,write):
        if not isinstance(sql,str) or not sql.strip() or len(sql)>16000:raise ValueError('Введите SQL до 16000 символов')
        if not isinstance(params,list) or len(params)>100 or any(type(x) not in {str,int,float,bool,type(None)} for x in params):raise ValueError('Параметры — JSON-массив простых значений, до100')
        started=time.monotonic();deadline=started+2;modified=False
        def authorize(action,a,b,database,trigger):
            nonlocal modified
            if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
            if action==sqlite3.SQLITE_READ:return sqlite3.SQLITE_OK if database in {'main',None} and a in self.TABLES|{'sqlite_master','sqlite_schema','json_each','json_tree'} else sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_FUNCTION:return sqlite3.SQLITE_DENY if (b or '').lower() in {'load_extension','readfile','writefile'} else sqlite3.SQLITE_OK
            if action==getattr(sqlite3,'SQLITE_RECURSIVE',33):return sqlite3.SQLITE_OK
            if action in {sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE} and database=='main' and a in self.MUTABLE and write:
                modified=True;return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        # Authorizer state belongs to this dedicated connection, not a live call.
        with self.store.lock:
            db=sqlite3.connect(self.store.folder/'sandbox.sqlite',timeout=2)
            db.row_factory=sqlite3.Row
            from .identity_form import folded,phone_digits
            db.create_function('clinic_name',1,folded);db.create_function('clinic_phone',1,phone_digits)
            db.create_function('clinic_part',2,lambda value,index:(str(value).split()+['','',''])[index])
            if hasattr(db,'setlimit'):
                db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,1024*1024)
                db.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH,100)
            db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            db.set_authorizer(authorize);db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            try:
                cursor=db.execute(sql,params);result=[dict(r) for r in cursor.fetchmany(201)] if cursor.description else []
                changed=db.total_changes;db.set_authorizer(None);db.commit()
            except sqlite3.Error as e:
                db.set_authorizer(None);db.rollback();raise ValueError('SQL не выполнен: '+str(e)) from None
            finally:db.close()
        return {'result':result[:200],'truncated':len(result)>200,'changed_rows':changed,'write':modified,'duration_ms':round((time.monotonic()-started)*1000),'source':'Референсная SQLite','reference':True,'network_requests':0}


class Workbench:
    def __init__(self,store,connections):
        self.store=store;self.connections=connections;self.sql=ReferenceSql(store)
        from app.mis.mock_stand import MockStand
        self.mis=MockStand(store.folder/'reference-mis.json')
    def public(self):
        value=self.connections.read()
        return {key:{**value[key],'reference':value[key]['adapter'].startswith('reference_')} for key in ('mis','database')}
    def request(self,route,data,identity):
        sql=route=='/api/workshop/sql';item=self.connections.read()['database' if sql else 'mis']
        reference=item['adapter'].startswith('reference_');staff=identity['role'] in {'admin','registrar'}
        write=data.get('confirm_write') is True
        if not staff and (not reference or write):raise ValueError('Для этого подключения нужен вход сотрудника')
        if sql:
            if reference:return self.sql.execute(data.get('sql'),data.get('params',[]),write)
            result=http_json(item,body={'sql':data.get('sql'),'params':data.get('params',[]),'confirm_write':write,'read_only':not write})
            return {'result':result.get('result',[]),'source':item['label'],'reference':False,'network_requests':1,'duration_ms':result.get('duration_ms',0),'changed_rows':result.get('changed_rows',0)}
        method=data.get('method','GET');path=data.get('path','/doctors');body=data.get('body');query=data.get('query',{})
        plan=lookup_http(method,path,body,query)
        if not plan['complete']:raise ValueError('Проверьте поля запроса: '+', '.join(plan['missing_fields']+plan['invalid_fields']))
        if method!='GET' and not (staff and write):raise ValueError('Подтвердите изменение данных сотрудником')
        started=time.monotonic()
        if reference:
            from app.types import MisError
            try:status,result=self.mis.handle(plan['method'],plan['path'],plan['query'],plan['body'])
            except MisError as e:raise ValueError(str(e)) from None
        else:status=200;result=http_json(item,plan['path'],method=plan['method'],body=plan['body'],query=plan['query'])
        return {'result':result,'http_status':status,'request':{k:plan[k] for k in ('method','path','query','body')},'source':item['label'],'reference':reference,'network_requests':0 if reference else 1,'duration_ms':round((time.monotonic()-started)*1000)}

"""Administrator-managed file/HTTP sources and explicit snapshot activation."""
from datetime import datetime, timezone
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import ipaddress
import http.client
import json
import mimetypes
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit,urlunsplit,quote
import urllib.error
import urllib.request
import zipfile

from .source_data import FILES, import_sources

KINDS = {'schedule':'Расписание врачей · Excel', 'appointments':'История приёмов · CSV',
         'call_events':'Журнал событий звонков · CSV', 'reference':'Справочная страница / API / текст'}
MAX_BYTES = 16 * 1024 * 1024


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in {'script','style','noscript','template'}:self.skip += 1
    def handle_endtag(self, tag):
        if tag in {'script','style','noscript','template'}:self.skip = max(0,self.skip-1)
    def handle_data(self, data):
        if not self.skip and data.strip():self.parts.append(data.strip())


def web_url(value):
    parts = urlsplit(value)
    if (parts.scheme not in {'http','https'} or not parts.hostname or parts.username is not None
        or parts.password is not None or any(ord(c)<33 for c in value)):
        raise ValueError('Укажите HTTP/HTTPS-ссылку без логина и пароля')
    try:parts.port
    except ValueError:raise ValueError('Некорректный порт источника') from None
    return parts


def check_destination(value):
    parts = web_url(value)
    if parts.hostname.lower() == 'metadata.google.internal':raise ValueError('Этот адрес не является источником данных')
    for info in socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme=='https' else 80),type=socket.SOCK_STREAM):
        address=ipaddress.ip_address(info[4][0])
        if address.is_link_local or address.is_unspecified or address.is_multicast:
            raise ValueError('Служебный сетевой адрес недоступен как источник')


class SourceRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_destination(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download(url):
    """Bounded public/internal HTTP GET; sends no module credentials or cookies."""
    try:check_destination(url)
    except OSError:raise ValueError('Не удалось определить адрес источника') from None
    parts=urlsplit(url)
    host=parts.hostname.encode('idna').decode()
    if ':' in host:host='['+host+']'
    authority=host+(':'+str(parts.port) if parts.port else '')
    url=urlunsplit((parts.scheme,authority,quote(parts.path,safe='/%:@!$&()*+,;=-._~'),quote(parts.query,safe='=&%/:?@!$()*+,;-._~'),''))
    request=urllib.request.Request(url,headers={'User-Agent':'Clinic-Source-Reader/1.0','Accept-Encoding':'identity'})
    opener=urllib.request.build_opener(SourceRedirect())
    try:
        with opener.open(request,timeout=4) as response:  # nosec B310 -- HTTP(S), including every redirect
            length=response.headers.get('Content-Length')
            if length and int(length)>MAX_BYTES:raise ValueError('Источник превышает 16 MiB')
            deadline=time.monotonic()+15
            chunks=[];size=0
            while True:
                if time.monotonic()>deadline:raise ValueError('Источник не ответил за отведённое время')
                chunk=response.read1(min(65536,MAX_BYTES+1-size))
                if not chunk:break
                size+=len(chunk)
                if size>MAX_BYTES:raise ValueError('Источник превышает 16 MiB')
                chunks.append(chunk)
            return b''.join(chunks),response.headers.get_content_type(),response.headers.get_content_charset() or 'utf-8',response.url
    except urllib.error.HTTPError as exc:
        raise ValueError(f'Источник вернул HTTP {exc.code}') from None
    except (OSError,urllib.error.URLError,http.client.HTTPException):
        raise ValueError('Не удалось прочитать источник; проверьте адрес и доступность') from None


class DataSources:
    def __init__(self, folder, store, roots, flat_roots=()):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'sources.json';self.store=store
        self.roots=[Path(root).resolve() for root in roots]
        self.flat_roots={Path(root).resolve() for root in flat_roots}
        self.lock=threading.RLock();self.operation=threading.Lock()
        self.seed=[]
        for kind,filename in FILES.items():
            candidate=next((root/filename for root in self.roots if (root/filename).is_file()),None)
            self.seed.append({'id':kind,'name':KINDS[kind],'kind':kind,'location':str(candidate or self.roots[0]/filename),'enabled':True})
        active=self.folder/'active.json'
        if active.exists():
            saved=json.loads(active.read_text())
            snapshot=(self.folder/saved['snapshot']).resolve()
            if snapshot.parent!=self.folder.resolve() or not snapshot.is_file():raise ValueError('Активный снимок источников недоступен')
            self.store.switch_source(snapshot)
        self._publish_references()

    def _publish_references(self):
        sources=self._read()['sources']
        path=self.folder/'status.json'
        statuses=json.loads(path.read_text()) if path.exists() else {}
        references=[]
        for item in sources:
            status=statuses.get(item['id'],{})
            if item['enabled'] and item['kind']=='reference' and status.get('status')=='loaded' and status.get('location')==item['location']:
                references.append({key:status.get(key) for key in ('name','location','checked_at','sha256')})
        with self.store.lock:self.store.reference_sources=references

    def _read(self):
        document=json.loads(self.path.read_text()) if self.path.exists() else {'sources':deepcopy(self.seed)}
        raw=json.dumps(document,ensure_ascii=False,sort_keys=True)
        return {**document,'revision':hashlib.sha256(raw.encode()).hexdigest()}

    def read(self):
        with self.lock:
            result=self._read()
            statuses=self.folder/'status.json'
            loaded=json.loads(statuses.read_text()) if statuses.exists() else {}
            result['status']={item['id']:loaded[item['id']] for item in result['sources'] if item['id'] in loaded and loaded[item['id']].get('location')==item['location']}
            result['kinds']=KINDS;result['active']=self.store.source_info()
            return result

    def local_path(self, value):
        path=Path(value).expanduser().resolve()
        if not any(path.is_relative_to(root) and (root not in self.flat_roots or path.parent==root) for root in self.roots):
            raise ValueError('Локальный файл должен находиться в каталоге исходных файлов или /imports')
        if path.suffix.lower() not in {'.csv','.xlsx','.json','.txt','.html','.htm'}:
            raise ValueError('Поддерживаются CSV, XLSX, JSON, TXT и HTML')
        return path

    def save(self, sources, revision):
        if not isinstance(sources,list) or not 1<=len(sources)<=32:raise ValueError('Укажите от 1 до 32 источников')
        checked=[];ids=set()
        for item in sources:
            if not isinstance(item,dict) or set(item)!={'id','name','kind','location','enabled'}:raise ValueError('Некорректное описание источника')
            if not isinstance(item['id'],str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}',item['id']) or item['id'] in ids:raise ValueError('Идентификаторы источников должны быть уникальны')
            ids.add(item['id'])
            if not isinstance(item['kind'],str) or item['kind'] not in KINDS or type(item['enabled']) is not bool:raise ValueError('Выберите назначение и состояние источника')
            if not isinstance(item['name'],str) or not 1<=len(item['name'].strip())<=160:raise ValueError('Укажите название источника')
            location=item['location']
            if not isinstance(location,str) or not 1<=len(location)<=2048:raise ValueError('Укажите путь или ссылку источника')
            if urlsplit(location).scheme in {'http','https'}:web_url(location)
            elif urlsplit(location).scheme:raise ValueError('Поддерживаются только HTTP/HTTPS-ссылки и локальные пути')
            else:self.local_path(location)
            checked.append({**item,'name':item['name'].strip()})
        with self.operation,self.lock:
            previous=self._read()
            if revision!=previous['revision']:raise ValueError('Источники изменены другим администратором. Обновите список.')
            self._atomic(self.path,{'sources':checked})
            removed={s['id'] for s in previous['sources']}-ids
            for key in removed:
                for suffix in ('.txt','.csv','.xlsx','.download'):(self.folder/(key+suffix)).unlink(missing_ok=True)
            status_path=self.folder/'status.json'
            if removed and status_path.exists():
                statuses=json.loads(status_path.read_text())
                self._atomic(status_path,{key:value for key,value in statuses.items() if key not in removed})
            self._publish_references()
            return self.read()

    @staticmethod
    def _atomic(path, value):
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2))
        temporary.replace(path)

    def _load(self,item):
        location=item['location']
        if urlsplit(location).scheme in {'http','https'}:
            raw,mime,charset,final=download(location)
        else:
            path=self.local_path(location)
            if not path.is_file():raise ValueError('Файл источника не найден')
            if path.stat().st_size>MAX_BYTES:raise ValueError('Источник превышает 16 MiB')
            raw=path.read_bytes();mime=mimetypes.guess_type(path.name)[0] or 'application/octet-stream';charset='utf-8-sig';final=location
        if not raw:raise ValueError('Источник пуст')
        if len(raw)>MAX_BYTES:raise ValueError('Источник превышает 16 MiB')
        extension='.xlsx' if item['kind']=='schedule' else '.csv' if item['kind'] in {'appointments','call_events'} else '.txt'
        if item['kind']=='schedule':
            import io
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    if sum(i.file_size for i in archive.infolist())>256*1024*1024:raise ValueError('Распакованное расписание превышает 256 MiB')
                    if '[Content_Types].xml' not in archive.namelist():raise ValueError('Ожидается Excel XLSX')
            except zipfile.BadZipFile:raise ValueError('По ссылке не найдено расписание XLSX') from None
        preview=''
        if item['kind']!='schedule':
            try:preview=raw.decode(charset)
            except (UnicodeError,LookupError):raise ValueError('Ожидается текст UTF-8 или корректная кодировка HTTP-ответа') from None
            if mime=='text/html' or re.match(r'\s*<(?:!doctype|html)',preview,re.I):
                if item['kind']!='reference':raise ValueError('Ссылка ведёт на HTML-страницу. Укажите прямую ссылку на CSV или выберите справочный источник.')
                parser=PageText();parser.feed(preview);preview='\n'.join(parser.parts)
                if len(preview.strip())<20:preview='Страница почти не содержит текста; возможно, данные загружаются JavaScript. Нужна прямая ссылка на данные или API.'
            if mime=='application/json':
                try:preview=json.dumps(json.loads(preview.lstrip("\ufeff")),ensure_ascii=False,indent=2)
                except ValueError:raise ValueError('Источник вернул некорректный JSON') from None
        path=self.folder/(item['id']+extension)
        temporary=path.with_suffix('.download');temporary.write_bytes(raw);temporary.replace(path)
        result={'location':location,'final_url':final,'checked_at':datetime.now(timezone.utc).isoformat(),
                'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'content_type':mime,'preview':preview[:12000],
                'status':'loaded','name':item['name'],'kind':item['kind']}
        return path,result

    def _status(self,key,result):
        path=self.folder/'status.json'
        values=json.loads(path.read_text()) if path.exists() else {}
        values[key]=result;self._atomic(path,values)
        self._publish_references()

    def inspect(self,key,revision):
        with self.operation:
            document=self._read()
            if revision!=document['revision']:raise ValueError('Сохраните изменения перед проверкой источника')
            item=next((s for s in document['sources'] if s['id']==key),None)
            if not item:raise ValueError('Источник не найден')
            try:
                _,result=self._load(item);self._status(key,result)
            except ValueError as exc:
                self._status(key,{'location':item['location'],'status':'error','error':str(exc),'checked_at':datetime.now(timezone.utc).isoformat()})
                raise
            return result

    def apply(self,revision):
        if not self.operation.acquire(blocking=False):raise ValueError('Дождитесь текущей проверки источника')
        try:
            document=self._read()
            if revision!=document['revision']:raise ValueError('Сохраните изменения перед обновлением данных')
            paths={};origins={}
            for kind in FILES:
                items=[s for s in document['sources'] if s['enabled'] and s['kind']==kind]
                if len(items)!=1:raise ValueError('Для обновления нужен ровно один включённый источник: '+KINDS[kind])
                path,status=self._load(items[0]);self._status(items[0]['id'],status)
                paths[kind]=path;origins[kind]=items[0]['location']
            snapshot=self.folder/('snapshot-'+secrets.token_hex(12)+'.sqlite')
            try:
                report=import_sources(self.folder,snapshot,source_paths=paths,origins=origins)
                self.store.switch_source(snapshot)
            except ImportError:
                snapshot.unlink(missing_ok=True)
                raise ValueError('На сервере не установлен модуль импорта таблиц') from None
            except (ValueError,KeyError,TypeError,OSError,sqlite3.Error,zipfile.BadZipFile):
                snapshot.unlink(missing_ok=True)
                raise ValueError('Данные не обновлены: структура источников не соответствует выбранному назначению') from None
            self._atomic(self.folder/'active.json',{'snapshot':snapshot.name,'revision':revision})
            # Keep the active snapshot and its predecessor for review/recovery.
            snapshots=sorted(self.folder.glob('snapshot-*.sqlite'),key=lambda p:p.stat().st_mtime,reverse=True)
            for old in snapshots[2:]:old.unlink()
            return {'status':'applied','report':report,'active':self.store.source_info()}
        finally:self.operation.release()

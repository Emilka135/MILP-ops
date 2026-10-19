"""Optional scoped response events. Compilation cannot bypass booking gates."""
import hashlib
import json


def scope_for(identity, data):
    if identity['role']=='registrar':return 'line:'+str(identity['registrar'])
    scope=data.get('scope','global')
    if scope!='global' and not (scope.startswith('line:') and scope[5:].isdigit()):raise ValueError('Укажите global или line:номер')
    return scope


def request(store, settings, identity, op, data):
    scope=scope_for(identity,data)
    if not settings.read()['scenario_extensions']['enabled']:raise ValueError('Модуль расширений сценариев отключён')
    with store.lock,store.db:
        if op=='add':
            phrase=str(data.get('phrase','')).strip();response=str(data.get('response','')).strip();state=str(data.get('state','menu'))
            from .demo_call import TITLES,TERMINAL
            if not 1<=len(phrase)<=300 or not 1<=len(response)<=1000 or state not in set(TITLES)-TERMINAL:raise ValueError('Нужны фраза, ответ и допустимый шаг')
            store.db.execute('INSERT INTO events(scope,phrase,response,state,author) VALUES (?,?,?,?,?)',(scope,phrase,response,state,identity['username']))
        if op=='remove':
            store.db.execute('DELETE FROM events WHERE id=? AND scope=?',(int(data['id']),scope))
        if op=='compile':
            events=[dict(r) for r in store.db.execute('SELECT * FROM events WHERE scope=? ORDER BY id',(scope,))]
            pairs=[(e['state'],e['phrase'].casefold()) for e in events]
            if len(set(pairs))!=len(pairs):raise ValueError('Повторяющиеся события: исправьте одинаковые фразы в одном шаге')
            doc=json.dumps(events,ensure_ascii=False,sort_keys=True)
            rev=hashlib.sha256(doc.encode()).hexdigest()
            store.db.execute('INSERT INTO extensions VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET revision=excluded.revision,document=excluded.document',(scope,rev,doc))
        row=store.db.execute('SELECT revision FROM extensions WHERE scope=?',(scope,)).fetchone()
        from .demo_call import TITLES,TERMINAL
        return {'scope':scope,'states':{k:v for k,v in TITLES.items() if k not in TERMINAL},'events':[dict(r) for r in store.db.execute('SELECT * FROM events WHERE scope=? ORDER BY id',(scope,))],
                'revision':row['revision'] if row else None,'effect':'Ответы применяются к новым тестовым звонкам. Шаг и обязательные проверки сохраняются.'}


def snapshot(store, line):
    with store.lock:
        rows=store.db.execute('SELECT scope,revision,document FROM extensions WHERE scope IN (?,?)',('global','line:'+str(line))).fetchall()
    return [{'scope':r['scope'],'revision':r['revision'],'events':json.loads(r['document'])} for r in rows]

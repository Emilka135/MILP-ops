"""Human-labelled call cases, overnight preparation and scoped, expiring experiments."""
from datetime import datetime, timedelta
import hashlib
import json
import re
import threading
import unicodedata

SCHEMA='''
CREATE TABLE IF NOT EXISTS learning_calls(call_id TEXT PRIMARY KEY,registrar TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,finished INTEGER NOT NULL,outcome TEXT NOT NULL,duration_s REAL NOT NULL,transcript TEXT NOT NULL,trace TEXT NOT NULL,experiment_id INTEGER);
CREATE TABLE IF NOT EXISTS learning_graphs(revision TEXT PRIMARY KEY,graph TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_cases(id INTEGER PRIMARY KEY,registrar TEXT NOT NULL,call_id TEXT NOT NULL,source_state TEXT NOT NULL,title TEXT NOT NULL,intent TEXT NOT NULL,examples TEXT NOT NULL,response TEXT NOT NULL,earlier_state TEXT,status TEXT NOT NULL DEFAULT 'queued',created_at TEXT NOT NULL,effective_at TEXT NOT NULL,expires_at TEXT NOT NULL,reviewed_at TEXT,report TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS learning_labels(id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL,call_id TEXT NOT NULL,registrar TEXT NOT NULL,user_text TEXT NOT NULL,correct_intent TEXT NOT NULL,correct_response TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(case_id,user_text));
'''
INTENTS={'book','reschedule','cancel','confirm','my_booking','operator','info_schedule','info_holidays','info_address','info_docs','referral','medical','emergency','complaint'}


def normalize(text):
    return ' '.join(re.findall(r'[a-zа-яё0-9]+',unicodedata.normalize('NFKC',str(text)).lower().replace('ё','е')))


def number(value):
    result=re.sub(r'[^0-9+]','',str(value))
    if not 1<=len(result)<=24:raise ValueError('Нужен номер регистратора')
    return result


class Learning:
    def __init__(self,reg):
        self.reg=reg
        self.lock=threading.RLock()
        reg._conn.executescript(SCHEMA)
        if 'scenario_revision' not in {r['name'] for r in reg._q('PRAGMA table_info(learning_calls)')}:
            reg._x("ALTER TABLE learning_calls ADD COLUMN scenario_revision TEXT NOT NULL DEFAULT ''")

    def handle(self,op,args,at,closed=False):
        with self.lock:return self._handle(op,args,at,closed)

    def _handle(self,op,args,at,closed):
        if op=='learning.archive':
            registrar=number(args.get('registrar') or '100')
            existing=self.reg.execute('SELECT registrar FROM learning_calls WHERE call_id=?',(str(args['call_id'])[:128],))
            if existing and existing[0]['registrar']!=registrar:return 'error',{'error':'Номер звонка уже принадлежит другому рабочему месту'}
            revision=str(args.get('scenario_revision') or '')
            graph=args.get('graph')
            if revision:
                if not re.fullmatch('[a-f0-9]{64}',revision):return 'error',{'error':'Invalid scenario revision'}
                if graph is not None:
                    if not isinstance(graph,dict) or not isinstance(graph.get('nodes'),dict):return 'error',{'error':'Invalid archived scenario graph'}
                    raw=json.dumps(graph,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                    if len(raw.encode())>2*1024*1024:return 'error',{'error':'Scenario graph too large'}
                    saved=self.reg._q('SELECT graph FROM learning_graphs WHERE revision=?',(revision,))
                    if saved and saved[0]['graph']!=raw:return 'error',{'error':'Scenario revision is immutable'}
                    self.reg._x('INSERT OR IGNORE INTO learning_graphs VALUES(?,?)',(revision,raw))
                if not self.reg._q('SELECT revision FROM learning_graphs WHERE revision=?',(revision,)):return 'error',{'error':'Scenario graph missing'}
            turns=args.get('transcript',[])[:500]
            transcript=[]
            for turn in turns:
                text=str(turn.get('text',''))[:4000]
                # Call training never needs full phone/SNILS/account-like digit strings.
                text=re.sub(r'(?<!\d)(?:\d[ -]?){7,}(?!\d)','[скрытый номер]',text)
                clean={'role':str(turn.get('role',''))[:20],'text':text,'state':str(turn.get('state',''))[:120]}
                if type(turn.get('step_index')) is int and 0<=turn['step_index']<500:clean['step_index']=turn['step_index']
                if turn.get('source'):clean['source']=str(turn['source'])[:40]
                transcript.append(clean)
            trace=[str(x)[:120] for x in args.get('trace',[])[:500]]
            call_id=str(args['call_id'])[:128]
            self.reg._x('''INSERT INTO learning_calls(call_id,registrar,created_at,updated_at,finished,outcome,duration_s,transcript,trace,experiment_id,scenario_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET updated_at=excluded.updated_at,finished=excluded.finished,outcome=excluded.outcome,duration_s=excluded.duration_s,transcript=excluded.transcript,trace=excluded.trace,experiment_id=excluded.experiment_id,scenario_revision=excluded.scenario_revision''',
                (call_id,registrar,at.isoformat(),at.isoformat(),int(bool(args.get('finished'))),str(args.get('outcome',''))[:50],max(0,float(args.get('duration_s',0))),json.dumps(transcript,ensure_ascii=False),json.dumps(trace),args.get('experiment_id'),revision))
            return 'ok',{}
        if op=='learning.calls':
            registrar=number(args['registrar'])
            return 'ok',{'calls':self.reg.execute('SELECT call_id,created_at,finished,outcome,duration_s,experiment_id,updated_at,scenario_revision FROM learning_calls WHERE registrar=? ORDER BY updated_at DESC LIMIT 100',(registrar,))}
        if op=='learning.call':
            rows=self.reg.execute('SELECT * FROM learning_calls WHERE call_id=? AND registrar=?',(str(args['call_id']),number(args['registrar'])))
            if not rows:return 'empty',{}
            row=rows[0];row['transcript']=json.loads(row['transcript']);row['trace']=json.loads(row['trace'])
            graphs=self.reg._q('SELECT graph FROM learning_graphs WHERE revision=?',(row.get('scenario_revision',''),))
            if graphs:row['graph']=json.loads(graphs[0]['graph'])
            return 'ok',row
        if op=='learning.propose':
            registrar=number(args['registrar'])
            status,call=self._handle('learning.call',{'registrar':registrar,'call_id':args['call_id']},at,closed)
            if status!='ok':return 'error',{'error':'Звонок не принадлежит этому рабочему месту'}
            state=str(args['source_state'])
            if state not in call['trace']:return 'error',{'error':'Этот шаг не встречался в звонке'}
            intent=str(args['intent'])
            if intent not in INTENTS:return 'error',{'error':'Выберите доступную тему обращения'}
            examples=list(dict.fromkeys(str(x).strip() for x in args.get('examples',[]) if str(x).strip()))
            if not 1<=len(examples)<=20 or any(len(x)>400 for x in examples):return 'error',{'error':'Добавьте от 1 до 20 коротких формулировок'}
            response=str(args.get('response','')).strip()
            title=str(args.get('title','')).strip()
            if not title or len(title)>160 or not response or len(response)>1500:return 'error',{'error':'Нужны короткое название и правильный ответ'}
            earlier=str(args.get('earlier_state') or '')
            if earlier and (earlier not in call['trace'] or call['trace'].index(earlier)>=call['trace'].index(state)):
                return 'error',{'error':'Ранний ответ должен находиться раньше в этом разговоре'}
            tomorrow=(at+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            expires=tomorrow+timedelta(days=7)
            with self.reg._lock:
                self.reg._conn.execute('BEGIN IMMEDIATE')
                try:
                    case_id=self.reg._x('INSERT INTO learning_cases(registrar,call_id,source_state,title,intent,examples,response,earlier_state,created_at,effective_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (registrar,args['call_id'],state,title,intent,json.dumps(examples,ensure_ascii=False),response,earlier or None,at.isoformat(),tomorrow.isoformat(),expires.isoformat()))
                    for text in examples:self.reg._x('INSERT INTO learning_labels(case_id,call_id,registrar,user_text,correct_intent,correct_response,created_at) VALUES(?,?,?,?,?,?,?)',(case_id,args['call_id'],registrar,text,intent,response,at.isoformat()))
                except Exception:
                    self.reg._conn.rollback()
                    raise
                else:
                    self.reg._conn.commit()
            return 'ok',{'case_id':case_id,'status':'queued','effective_at':tomorrow.isoformat(),'expires_at':expires.isoformat(),'achievement':'first_example','message':'Пример сохранён. Ночью проверим, затем администратор решит, можно ли испытать его на вашем номере.'}
        if op=='learning.cases':
            rows=self.reg.execute('SELECT * FROM learning_cases WHERE registrar=? ORDER BY id DESC',(number(args['registrar']),))
            for row in rows:row['examples']=json.loads(row['examples']);row['report']=json.loads(row['report'])
            labels=self.reg.execute('SELECT COUNT(*) AS n FROM learning_labels WHERE registrar=?',(number(args['registrar']),))[0]['n']
            return 'ok',{'cases':rows,'labelled_examples':labels,'badges':{'first_example':labels>0,'night_checked':any(x['status'] in {'prepared','approved','active'} for x in rows),'in_use':any(x['status']=='active' for x in rows)}}
        if op=='learning.night':
            if not closed:return 'blocked',{'reason':'clinic_is_open','processed':0}
            rows=self.reg.execute("SELECT * FROM learning_cases WHERE status='queued' ORDER BY id LIMIT 100")
            for row in rows:
                examples=json.loads(row['examples'])
                # The initial supervised model is an exact normalized phrase table, with labelled topics.
                report={'method':'supervised_normalized_examples','examples':len(examples),'topic':row['intent'],'neural_training':False,'activation':'admin_review_required','needs_order_review':bool(row['earlier_state']),'fingerprint':hashlib.sha256(json.dumps([row['source_state'],examples,row['intent'],row['response']],ensure_ascii=False).encode()).hexdigest()}
                self.reg._x("UPDATE learning_cases SET status='prepared',report=? WHERE id=?",(json.dumps(report,ensure_ascii=False),row['id']))
            return 'ok',{'processed':len(rows),'method':'local_supervised_examples','topics':len({r['intent'] for r in rows})}
        if op=='learning.review':
            rows=self.reg.execute('SELECT * FROM learning_cases WHERE id=?',(int(args['id']),))
            if not rows:return 'empty',{}
            row=rows[0]
            if args.get('approve') is not True:
                self.reg._x("UPDATE learning_cases SET status='rejected',reviewed_at=? WHERE id=?",(at.isoformat(),row['id']));return 'ok',{}
            if row['status']!='prepared':return 'blocked',{'error':'Сначала нужна ночная проверка'}
            # No arbitrary string is proof of a validated, deployable scenario.
            return 'blocked',{'error':'Развёртывание персональных версий ещё не подключено. Предложение сохранено для проверки.'}
        if op=='learning.alias':
            registrar=number(args['registrar'])
            self.reg._x("UPDATE learning_cases SET status='expired' WHERE expires_at<=? AND status IN ('approved','active')",(at.isoformat(),))
            if self._complaints(registrar):
                self.reg._x("UPDATE learning_cases SET status='suspended' WHERE registrar=? AND status IN('approved','active')",(registrar,));return 'empty',{}
            rows=self.reg.execute("SELECT * FROM learning_cases WHERE registrar=? AND source_state=? AND status IN('approved','active') AND effective_at<=? AND expires_at>? ORDER BY id DESC",(registrar,str(args['state']),at.isoformat(),at.isoformat()))
            text=normalize(args['text']);matches=[r for r in rows if text and text in {normalize(x) for x in json.loads(r['examples'])}]
            if len({r['intent'] for r in matches})>1:return 'empty',{'reason':'conflicting_labels'}
            if not matches:return 'empty',{}
            row=matches[0];self.reg._x("UPDATE learning_cases SET status='active' WHERE id=?",(row['id'],))
            return 'ok',{'intent':row['intent'],'experiment_id':row['id'],'scope':'registrar_and_state','expires_at':row['expires_at']}
        return 'error',{'error':'unknown_learning_operation'}

    def _complaints(self,registrar):
        exists=self.reg.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='complaints'")
        return bool(exists and self.reg.execute("SELECT id FROM complaints WHERE registrar=? AND state='open'",(registrar,)))

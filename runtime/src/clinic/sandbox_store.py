"""Isolated, inspectable SQLite stand. Never connects to the live MIS."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


class SandboxStore:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.audio = self.folder / 'calls'
        self.audio.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.folder / 'sandbox.sqlite', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, birth_date TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS referrals(patient_id INTEGER, specialty TEXT, valid_until TEXT);
        CREATE TABLE IF NOT EXISTS slots(id INTEGER PRIMARY KEY, specialty TEXT, doctor TEXT, starts_at TEXT, synced_at TEXT);
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, call_id TEXT UNIQUE, patient_id INTEGER NOT NULL, slot_id INTEGER UNIQUE NOT NULL, confirmed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS queries(id INTEGER PRIMARY KEY, call_id TEXT, sql TEXT, params TEXT, result TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, owner TEXT, created_at TEXT, updated_at TEXT, document TEXT);
        CREATE TABLE IF NOT EXISTS client_tags(patient_id INTEGER PRIMARY KEY, tags TEXT);
        CREATE TABLE IF NOT EXISTS preferences(patient_id INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, scope TEXT, phrase TEXT, response TEXT, state TEXT, author TEXT);
        CREATE TABLE IF NOT EXISTS extensions(scope TEXT PRIMARY KEY, revision TEXT, document TEXT);
        ''')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO patients VALUES (1, 'Иванов Иван Иванович', '1985-04-12')")
            self.db.execute("INSERT OR IGNORE INTO patients VALUES (2, 'Петрова Анна Сергеевна', '1990-08-23')")

    def query(self, call_id, sql, params=()):
        """Only application-owned parameterized statements reach this method."""
        with self.lock, self.db:
            rows = [dict(row) for row in self.db.execute(sql, params).fetchall()]
            self.db.execute('INSERT INTO queries(call_id,sql,params,result,created_at) VALUES (?,?,?,?,?)',
                            (call_id, sql, json.dumps(params, ensure_ascii=False), json.dumps(rows, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
            return rows

    def patient(self, call_id, name, birth):
        return self.query(call_id, 'SELECT id,full_name,birth_date FROM patients WHERE lower(full_name)=lower(?) AND birth_date=?', (name, birth))

    def lookup_patient(self, call_id, name, birth):
        # SQLite lower() is ASCII-only. Unicode casefold belongs to the application.
        rows = self.query(call_id, 'SELECT id,full_name,birth_date FROM patients WHERE birth_date=?', (birth,))
        return next((p for p in rows if p['full_name'].casefold().replace('ё','е') == name.casefold().replace('ё','е')), None)

    def slots(self, call_id, specialty, preference=None):
        now = datetime.now()
        # Explicitly synthetic stand sync, visible in table; never a claim of external synchronization.
        with self.lock, self.db:
            for day in range(1, 4):
                for hour in (10,14,18):
                    start = (now + timedelta(days=day)).replace(hour=hour, minute=day*10, second=0, microsecond=0).isoformat()
                    existing = self.db.execute('SELECT id FROM slots WHERE specialty=? AND starts_at=?', (specialty,start)).fetchone()
                    if existing:
                        self.db.execute('UPDATE slots SET synced_at=? WHERE id=?', (now.isoformat(),existing['id']))
                    else:
                        self.db.execute('INSERT INTO slots(specialty,doctor,starts_at,synced_at) VALUES (?,?,?,?)', (specialty, 'Тестовый врач · '+specialty,start,now.isoformat()))
        ranges={'morning':('06:00','11:59'),'afternoon':('12:00','16:59'),'evening':('17:00','23:00')}
        lo,hi=ranges.get(preference,('00:00','23:59'))
        return self.query(call_id, 'SELECT * FROM slots WHERE specialty=? AND starts_at>? AND synced_at>? AND substr(starts_at,12,5) BETWEEN ? AND ? AND id NOT IN (SELECT slot_id FROM bookings) ORDER BY starts_at LIMIT 3',
                          (specialty,now.isoformat(),(now-timedelta(minutes=5)).isoformat(),lo,hi))

    def has_referral(self, call_id, patient, specialty):
        return bool(self.query(call_id, 'SELECT * FROM referrals WHERE patient_id=? AND specialty=? AND valid_until>=?', (patient,specialty,datetime.now().date().isoformat())))

    def book(self, call_id, patient, slot_id, specialty, referral_required):
        with self.lock:
            # Recheck all prerequisites inside one SQLite transaction; no client-supplied slot fields.
            try:
                self.db.execute('BEGIN IMMEDIATE')
                p = self.db.execute('SELECT * FROM patients WHERE id=?', (patient,)).fetchone()
                slot = self.db.execute('SELECT * FROM slots WHERE id=? AND specialty=? AND starts_at>? AND synced_at>?',
                    (slot_id,specialty,datetime.now().isoformat(),(datetime.now()-timedelta(minutes=5)).isoformat())).fetchone()
                referral = not referral_required or self.db.execute('SELECT 1 FROM referrals WHERE patient_id=? AND specialty=? AND valid_until>=?', (patient,specialty,datetime.now().date().isoformat())).fetchone()
                if not p or not p['full_name'] or not p['birth_date'] or not slot or not referral:
                    raise ValueError('Не подтверждены пациент, направление или актуальный слот')
                sql = 'INSERT INTO bookings(call_id,patient_id,slot_id,confirmed_at) VALUES (?,?,?,?)'
                params = (call_id,patient,slot_id,datetime.now().isoformat())
                cursor = self.db.execute(sql,params)
                result = {'id':cursor.lastrowid,'patient':dict(p),'slot':dict(slot),'sandbox':True}
                self.db.execute('INSERT INTO queries(call_id,sql,params,result,created_at) VALUES (?,?,?,?,?)',
                                (call_id,sql,json.dumps(params),json.dumps(result,ensure_ascii=False),datetime.now().isoformat()))
                self.db.commit()
                return result
            except sqlite3.IntegrityError:
                self.db.rollback()
                raise ValueError('Этот талон уже занят. Выберите другое время.') from None
            except Exception:
                self.db.rollback()
                raise

    def save_call(self, key, owner, document):
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self.db:
            self.db.execute('INSERT INTO calls VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,document=excluded.document',
                            (key,owner,now,now,json.dumps(document,ensure_ascii=False)))
        # Human-readable text-only archive; audio is written separately only with consent.
        (self.audio / (key+'.json')).write_text(json.dumps(document,ensure_ascii=False,indent=2))

    def list_calls(self):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT id,created_at,updated_at FROM calls ORDER BY updated_at DESC LIMIT 100')]

    def call(self, key):
        with self.lock:
            row = self.db.execute('SELECT document FROM calls WHERE id=?',(key,)).fetchone()
        if not row: raise ValueError('Звонок не найден')
        return json.loads(row['document'])

    def tables(self):
        statements={
            'patients':'SELECT * FROM patients ORDER BY rowid DESC LIMIT 100',
            'referrals':'SELECT * FROM referrals ORDER BY rowid DESC LIMIT 100',
            'slots':'SELECT * FROM slots ORDER BY rowid DESC LIMIT 100',
            'bookings':'SELECT * FROM bookings ORDER BY rowid DESC LIMIT 100',
            'queries':'SELECT * FROM queries ORDER BY rowid DESC LIMIT 100',
            'client_tags':'SELECT * FROM client_tags ORDER BY rowid DESC LIMIT 100',
            'preferences':'SELECT * FROM preferences ORDER BY rowid DESC LIMIT 100',
        }
        with self.lock:
            return {'source':'Изолированная тестовая SQLite; внешняя МИС не синхронизируется', 'tables':{
                name:[dict(r) for r in self.db.execute(sql)] for name,sql in statements.items()}}

    def tag(self, patient, flags):
        with self.lock, self.db:
            self.db.execute('INSERT INTO client_tags VALUES (?,?) ON CONFLICT(patient_id) DO UPDATE SET tags=excluded.tags', (patient,json.dumps(flags)))

    def preference(self, patient, text):
        with self.lock, self.db:
            self.db.execute('INSERT INTO preferences VALUES (?,?) ON CONFLICT(patient_id) DO UPDATE SET value=excluded.value', (patient,text))

    def saved_preference(self, patient):
        with self.lock:
            row=self.db.execute('SELECT value FROM preferences WHERE patient_id=?',(patient,)).fetchone()
        return row['value'] if row else None

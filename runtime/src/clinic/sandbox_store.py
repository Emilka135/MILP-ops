"""Isolated, inspectable SQLite stand. Never connects to the live MIS."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def clinic_now():
    return datetime.now(ZoneInfo(os.environ.get("CLINIC_TIMEZONE","Europe/Moscow"))).replace(tzinfo=None)


class SandboxStore:
    def __init__(self, folder, source_path=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.audio = self.folder / 'calls'
        self.audio.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.folder / 'sandbox.sqlite', check_same_thread=False, uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.create_function('clinic_name',1,lambda value:' '.join(str(value).casefold().replace('ё','е').split()),deterministic=True)
        source_path=source_path or os.environ.get('CLINIC_SOURCE_DB') or ('/imports/clinic-source.sqlite' if Path('/imports/clinic-source.sqlite').is_file() else None)
        self.reference_sources=[]
        self.source_path=Path(source_path).resolve() if source_path else None
        if self.source_path:
            if not self.source_path.is_file():raise ValueError('Не найдена база исходных выгрузок. Выполните tools/import_clinic_sources.py')
            self.db.execute('ATTACH DATABASE ? AS source',(self.source_path.as_uri()+'?mode=ro',))

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
        for name,kind in [('oms','TEXT'),('district','TEXT')]:
            if name not in {r['name'] for r in self.db.execute('PRAGMA table_info(patients)')}:
                self.db.execute('ALTER TABLE patients ADD COLUMN '+name+' '+kind)
        self.db.execute('CREATE TABLE IF NOT EXISTS doctors(id INTEGER PRIMARY KEY,full_name TEXT NOT NULL,specialty TEXT NOT NULL,district TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS district_doctors(district TEXT,specialty TEXT,doctor_id TEXT,PRIMARY KEY(district,specialty,doctor_id))')
        if 'source_slot_id' not in {r['name'] for r in self.db.execute('PRAGMA table_info(slots)')}:
            self.db.execute('ALTER TABLE slots ADD COLUMN source_slot_id TEXT')

        columns={r['name'] for r in self.db.execute('PRAGMA table_info(calls)')}
        if 'registrar' not in columns:
            self.db.execute("ALTER TABLE calls ADD COLUMN registrar TEXT NOT NULL DEFAULT '100'")
        if 'call_kind' not in columns:
            self.db.execute("ALTER TABLE calls ADD COLUMN call_kind TEXT NOT NULL DEFAULT 'test'")
            self.db.execute("UPDATE calls SET call_kind='imported' WHERE json_extract(document,'$.analysis')=1")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO patients(id,full_name,birth_date,oms,district) VALUES (1, 'Иванов Иван Иванович', '1985-04-12','1234567890123456','14')")
            self.db.execute("INSERT OR IGNORE INTO patients(id,full_name,birth_date,oms,district) VALUES (2, 'Петрова Анна Сергеевна', '1990-08-23','2345678901234567','25')")

            # Explicit synthetic fixtures; never imported facts about a clinic.
            self.db.execute("UPDATE patients SET district='14' WHERE id=1 AND full_name='Иванов Иван Иванович' AND district IS NULL")
            self.db.execute("UPDATE patients SET district='25' WHERE id=2 AND full_name='Петрова Анна Сергеевна' AND district IS NULL")
            for row in [(1,'Тестовый врач Кузнецова Анна Сергеевна','therapist','14'),(2,'Тестовый врач Смирнова Ольга Викторовна','therapist','25'),(3,'Тестовый врач Петров Сергей Иванович','pediatrician','14'),(4,'Тестовый врач Иванова Мария Петровна','pediatrician','25')]:
                self.db.execute('INSERT OR IGNORE INTO doctors VALUES (?,?,?,?)',row)
            fixtures=json.loads(Path(__file__).with_name('sandbox_fixtures.json').read_text())
            self.db.executemany('INSERT OR IGNORE INTO patients(id,full_name,birth_date,oms,district) VALUES(?,?,?,?,?)',fixtures['patients'])
            self.db.executemany('INSERT OR IGNORE INTO doctors VALUES(?,?,?,?)',fixtures['doctors'])

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
        rows = self.query(call_id, 'SELECT id,full_name,birth_date,oms,district FROM patients WHERE clinic_name(full_name)=clinic_name(?) AND birth_date=?', (name,birth))
        return rows[0] if len(rows)==1 else None

    def lookup_policy(self,call_id,policy):
        rows=self.query(call_id,'SELECT id,full_name,birth_date,oms,district FROM patients WHERE oms=?',(policy,))
        return rows[0] if len(rows)==1 else None

    def switch_source(self,path):
        """Validate a new snapshot before replacing the attached read-only source."""
        path=Path(path).resolve()
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as check:
            for statement in ('SELECT * FROM sources LIMIT 0','SELECT * FROM doctors LIMIT 0','SELECT * FROM slots LIMIT 0','SELECT * FROM unavailability LIMIT 0','SELECT * FROM appointments LIMIT 0','SELECT * FROM call_events LIMIT 0'):
                check.execute(statement)
        with self.lock:
            previous=self.source_path
            if previous:self.db.execute('DETACH DATABASE source')
            try:self.db.execute('ATTACH DATABASE ? AS source',(path.as_uri()+'?mode=ro',))
            except Exception:
                if previous:self.db.execute('ATTACH DATABASE ? AS source',(previous.as_uri()+'?mode=ro',))
                raise
            self.source_path=path

    def source_info(self):
        if not self.source_path:return {'mode':'synthetic_fixture','label':'Изолированная синтетическая картотека для тестов','reference_sources':list(self.reference_sources)}
        with self.lock:
            sources=[dict(r) for r in self.db.execute('SELECT * FROM source.sources ORDER BY kind')]
        return {'mode':'file_snapshot','database':str(self.source_path),'sources':sources,'reference_sources':list(self.reference_sources),
                'limitations':['ФИО пациентов и даты рождения в выгрузках отсутствуют: идентификация проверяется на отдельной тестовой картотеке.',
                               'Привязки участков к врачам в файлах нет; автоматическая запись к участковому требует этой связи.',
                               'Это снимок файлов, не онлайн-синхронизация с МИС. CSV звонков содержит только события, без расшифровок.']}

    @staticmethod
    def _source_specialty(specialty):
        from .specialty_dictionary import SpecialtyDictionary,normalized
        policy=SpecialtyDictionary().policy(specialty)
        return sorted({normalized(value) for value in ([policy['label'],*policy['aliases']] if policy else [specialty])})

    def doctors(self,call_id,specialty,district=None):
        if self.source_path:
            labels=self._source_specialty(specialty)
            return self.query(call_id,'SELECT d.id,d.full_name,d.specialty,d.cabinet,d.patient_group FROM source.doctors d WHERE clinic_name(d.specialty) IN (SELECT value FROM json_each(?)) AND (? IS NULL OR EXISTS (SELECT 1 FROM district_doctors a WHERE a.doctor_id=d.id AND a.district=? AND a.specialty=?)) ORDER BY d.full_name',(json.dumps(labels,ensure_ascii=False),district,district,specialty))
        return self.query(call_id,'SELECT * FROM doctors WHERE specialty=? AND (? IS NULL OR district=?) ORDER BY full_name',(specialty,district,district))

    def queries(self,call_id):
        with self.lock:
            rows=self.db.execute('SELECT * FROM queries WHERE call_id=? ORDER BY id',(call_id,)).fetchall()
        return [{**dict(r),'params':json.loads(r['params']),'result':json.loads(r['result'])} for r in rows]

    def slots(self, call_id, specialty, preference=None, doctor=None):
        now = clinic_now()
        if self.source_path:
            labels=self._source_specialty(specialty)
            rows=self.query(call_id,"SELECT s.id AS source_slot_id,s.starts_at,s.status,d.full_name AS doctor,d.cabinet FROM source.slots s JOIN source.doctors d ON d.id=s.doctor_id WHERE clinic_name(d.specialty) IN (SELECT value FROM json_each(?)) AND s.status IN ('Свободно','Свободен') AND s.starts_at>=? AND s.starts_at<? AND (? IS NULL OR d.full_name=?) AND NOT EXISTS (SELECT 1 FROM source.unavailability u WHERE u.doctor_id=d.id AND substr(s.starts_at,1,10) BETWEEN u.starts_on AND u.ends_on) AND NOT EXISTS (SELECT 1 FROM slots local JOIN bookings b ON b.slot_id=local.id WHERE local.source_slot_id=s.id) ORDER BY s.starts_at LIMIT 3",
                            (json.dumps(labels,ensure_ascii=False),(now+timedelta(minutes=30)).isoformat(),(now+timedelta(days=14)).isoformat(),doctor,doctor))
            with self.lock,self.db:
                for row in rows:
                    self.db.execute('INSERT INTO slots(id,specialty,doctor,starts_at,synced_at,source_slot_id) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET doctor=excluded.doctor,starts_at=excluded.starts_at,synced_at=excluded.synced_at,source_slot_id=excluded.source_slot_id',
                        (1000000+int(row['source_slot_id'][1:]),specialty,row['doctor'],row['starts_at'],now.isoformat(),row['source_slot_id']))
            return self.query(call_id,'SELECT s.* FROM slots s WHERE s.source_slot_id IN (SELECT value FROM json_each(?)) AND s.id NOT IN (SELECT slot_id FROM bookings) ORDER BY s.starts_at',(json.dumps([r['source_slot_id'] for r in rows]),))
        # Explicitly synthetic stand sync, visible in table; never a claim of external synchronization.
        with self.lock, self.db:
            for day in range(1, 4):
                for hour in (10,14,18):
                    start = (now + timedelta(days=day)).replace(hour=hour, minute=day*10, second=0, microsecond=0).isoformat()
                    existing = self.db.execute('SELECT id FROM slots WHERE specialty=? AND starts_at=? AND doctor=?', (specialty,start,doctor or 'Тестовый врач · '+specialty)).fetchone()
                    if existing:
                        self.db.execute('UPDATE slots SET synced_at=? WHERE id=?', (now.isoformat(),existing['id']))
                    else:
                        self.db.execute('INSERT INTO slots(specialty,doctor,starts_at,synced_at) VALUES (?,?,?,?)', (specialty, doctor or 'Тестовый врач · '+specialty,start,now.isoformat()))
        ranges={'morning':('06:00','11:59'),'afternoon':('12:00','16:59'),'evening':('17:00','23:00')}
        lo,hi=ranges.get(preference,('00:00','23:59'))
        return self.query(call_id, 'SELECT * FROM slots WHERE specialty=? AND starts_at>? AND synced_at>? AND substr(starts_at,12,5) BETWEEN ? AND ? AND (? IS NULL OR doctor=?) AND id NOT IN (SELECT slot_id FROM bookings) ORDER BY starts_at LIMIT 3',
                          (specialty,now.isoformat(),(now-timedelta(minutes=5)).isoformat(),lo,hi,doctor,doctor))

    def has_referral(self, call_id, patient, specialty):
        return bool(self.query(call_id, 'SELECT * FROM referrals WHERE patient_id=? AND specialty=? AND valid_until>=?', (patient,specialty,clinic_now().date().isoformat())))

    def book(self, call_id, patient, slot_id, specialty, referral_required):
        with self.lock:
            # Recheck all prerequisites inside one SQLite transaction; no client-supplied slot fields.
            try:
                self.db.execute('BEGIN IMMEDIATE')
                p = self.db.execute('SELECT * FROM patients WHERE id=?', (patient,)).fetchone()
                slot = self.db.execute('SELECT * FROM slots WHERE id=? AND specialty=? AND starts_at>? AND synced_at>?',
                    (slot_id,specialty,clinic_now().isoformat(),(clinic_now()-timedelta(minutes=5)).isoformat())).fetchone()
                referral = not referral_required or self.db.execute('SELECT 1 FROM referrals WHERE patient_id=? AND specialty=? AND valid_until>=?', (patient,specialty,clinic_now().date().isoformat())).fetchone()
                if self.source_path and slot:
                    actual=self.db.execute("SELECT s.id FROM source.slots s JOIN source.doctors d ON d.id=s.doctor_id WHERE s.id=? AND s.starts_at=? AND d.full_name=? AND s.status IN ('Свободно','Свободен') AND NOT EXISTS (SELECT 1 FROM source.unavailability u WHERE u.doctor_id=s.doctor_id AND substr(s.starts_at,1,10) BETWEEN u.starts_on AND u.ends_on)",(slot['source_slot_id'],slot['starts_at'],slot['doctor'])).fetchone()
                    if not actual:raise ValueError('Талон не подтверждён исходным расписанием')
                if not p or not p['full_name'] or not p['birth_date'] or not slot or not referral:
                    raise ValueError('Не подтверждены пациент, направление или актуальный слот')
                sql = 'INSERT INTO bookings(call_id,patient_id,slot_id,confirmed_at) VALUES (?,?,?,?)'
                params = (call_id,patient,slot_id,clinic_now().isoformat())
                cursor = self.db.execute(sql,params)
                result = {'id':cursor.lastrowid,'patient':dict(p),'slot':dict(slot),'sandbox':True}
                self.db.execute('INSERT INTO queries(call_id,sql,params,result,created_at) VALUES (?,?,?,?,?)',
                                (call_id,sql,json.dumps(params),json.dumps(result,ensure_ascii=False),clinic_now().isoformat()))
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
            self.db.execute('INSERT INTO calls(id,owner,created_at,updated_at,document,registrar,call_kind) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,document=excluded.document,call_kind=excluded.call_kind',
                            (key,owner,now,now,json.dumps(document,ensure_ascii=False),str(document.get("registrar") or "100"),self.kind(document)))
        # Human-readable text-only archive; audio is written separately only with consent.
        (self.audio / (key+'.json')).write_text(json.dumps(document,ensure_ascii=False,indent=2))

    @staticmethod
    def kind(document):
        if document.get('analysis'):return 'imported'
        sources={t.get('source') for t in document.get('transcript',[])}
        return 'audio' if 'real_asr' in sources else 'text' if 'typed_test' in sources else 'test'

    def list_calls(self,registrar=None,metadata=False):
        with self.lock:
            statement='SELECT * FROM calls ORDER BY updated_at DESC LIMIT 100' if registrar is None else 'SELECT * FROM calls WHERE registrar=? ORDER BY updated_at DESC LIMIT 100'
            rows=self.db.execute(statement,(registrar,) if registrar is not None else ()).fetchall()
        if not metadata:return [{k:r[k] for k in ('id','created_at','updated_at')} for r in rows]
        result=[]
        for row in rows:
            doc=json.loads(row['document'])
            result.append({'id':row['id'],'call_id':row['id'],'created_at':row['created_at'],'updated_at':row['updated_at'],
                'owner':row['owner'],'registrar':row['registrar'],'call_kind':self.kind(doc),'kind':self.kind(doc),
                'finished':doc.get('finished',False),'outcome':doc.get('state',''),'duration_s':doc.get('duration_s'),
                'turn_count':len(doc.get('transcript',[]))})
        return result

    def call_metadata(self,key):
        with self.lock:
            row=self.db.execute('SELECT id,owner,registrar,call_kind FROM calls WHERE id=?',(key,)).fetchone()
        return dict(row) if row else None

    def call(self, key):
        with self.lock:
            row = self.db.execute('SELECT document FROM calls WHERE id=?',(key,)).fetchone()
        if not row: raise ValueError('Звонок не найден')
        return json.loads(row['document'])

    def tables(self):
        statements={
            'patients':'SELECT * FROM patients ORDER BY rowid DESC LIMIT 100',
            'doctors':'SELECT * FROM doctors ORDER BY id LIMIT 100',
            'referrals':'SELECT * FROM referrals ORDER BY rowid DESC LIMIT 100',
            'slots':'SELECT * FROM slots ORDER BY rowid DESC LIMIT 100',
            'bookings':'SELECT * FROM bookings ORDER BY rowid DESC LIMIT 100',
            'queries':'SELECT * FROM queries ORDER BY rowid DESC LIMIT 100',
            'client_tags':'SELECT * FROM client_tags ORDER BY rowid DESC LIMIT 100',
            'preferences':'SELECT * FROM preferences ORDER BY rowid DESC LIMIT 100',
        }
        if self.source_path:
            statements.update(doctors='SELECT * FROM source.doctors ORDER BY id LIMIT 100',source_slots='SELECT * FROM source.slots ORDER BY starts_at DESC LIMIT 100',source_appointments='SELECT * FROM source.appointments ORDER BY day DESC LIMIT 100',source_call_events='SELECT * FROM source.call_events ORDER BY at DESC LIMIT 100')
        with self.lock:
            return {'source':'SQLite со снимком исходных CSV и Excel; внешняя МИС не синхронизируется' if self.source_path else 'Изолированная тестовая SQLite; внешняя МИС не синхронизируется', 'tables':{
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

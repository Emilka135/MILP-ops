"""Import the supplied exports verbatim into an inspectable SQLite snapshot."""
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

FILES={'appointments':'30aef650-1a0b-4238-8c14-2dba969a1a2c.csv',
       'call_events':'21de07bd-6bc7-4d24-9aa1-15f9f32fd12a.csv',
       'schedule':'Расписание_врачей_25.06-31.12.2026.xlsx'}


def import_sources(source_root,output):
    import openpyxl
    source_root=Path(source_root).resolve();output=Path(output)
    paths={kind:source_root/name for kind,name in FILES.items()}
    for path in paths.values():
        if not path.is_file():raise FileNotFoundError(path)
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_suffix('.building.sqlite');temporary.unlink(missing_ok=True)
    db=sqlite3.connect(temporary)
    db.executescript('''
    CREATE TABLE sources(kind TEXT PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL,imported_at TEXT NOT NULL,row_count INTEGER NOT NULL);
    CREATE TABLE appointments(booking_id TEXT PRIMARY KEY,patient_hash TEXT,day TEXT,at TEXT,doctor TEXT,specialty TEXT,status TEXT);
    CREATE TABLE call_events(call_id TEXT PRIMARY KEY,at TEXT,wait_sec INTEGER,duration_sec INTEGER,result TEXT,operator_ext TEXT,caller_number TEXT);
    CREATE TABLE doctors(id TEXT PRIMARY KEY,full_name TEXT,specialty TEXT,cabinet TEXT,patient_group TEXT,slot_minutes INTEGER);
    CREATE TABLE slots(id TEXT PRIMARY KEY,doctor_id TEXT,starts_at TEXT,ends_at TEXT,status TEXT);
    CREATE TABLE unavailability(doctor_id TEXT,starts_on TEXT,ends_on TEXT,reason TEXT);
    CREATE INDEX slot_search ON slots(doctor_id,status,starts_at);
    CREATE INDEX doctor_specialty ON doctors(specialty);
    ''')
    counts={};at=datetime.now(timezone.utc).isoformat()
    try:
        with paths['appointments'].open(encoding='utf-8-sig',newline='') as file:
            reader=csv.DictReader(file,delimiter=';')
            db.executemany('INSERT INTO appointments VALUES(?,?,?,?,?,?,?)',((r['ID записи'],r['Пациент (хэш)'],datetime.strptime(r['Дата приёма'],'%d.%m.%Y').date().isoformat(),r['Время приёма'],r['Специалист'],r['Специальность'],r['Статус']) for r in reader))
        with paths['call_events'].open(encoding='utf-8-sig',newline='') as file:
            reader=csv.DictReader(file,delimiter=';')
            db.executemany('INSERT INTO call_events VALUES(?,?,?,?,?,?,?)',((r['call_id'],r['datetime'],int(r['wait_sec']),int(r['duration_sec']),r['result'],r['operator_ext'],r['caller_number']) for r in reader))
        wb=openpyxl.load_workbook(paths['schedule'],read_only=True,data_only=True)
        try:
            db.executemany('INSERT INTO doctors VALUES(?,?,?,?,?,?)',((r[0],r[1],r[3],str(r[5]),r[4],int(r[6])) for r in wb['Врачи'].iter_rows(min_row=2,values_only=True) if r[0]))
            db.executemany('INSERT INTO slots VALUES(?,?,?,?,?)',((r[0],r[3],datetime.combine(r[1].date(),r[8]).isoformat(),datetime.combine(r[1].date(),r[9]).isoformat(),r[11]) for r in wb['Слоты'].iter_rows(min_row=2,values_only=True) if r[0]))
            db.executemany('INSERT INTO unavailability VALUES(?,?,?,?)',((r[0],r[4].date().isoformat(),r[5].date().isoformat(),r[3]) for r in wb['Недоступность'].iter_rows(min_row=2,values_only=True) if r[0]))
        finally:wb.close()
        for table in ('appointments','call_events','doctors','slots','unavailability'):counts[table]=db.execute('SELECT count(*) FROM '+table).fetchone()[0]
        for kind,path in paths.items():
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?)',(kind,str(path),hashlib.sha256(path.read_bytes()).hexdigest(),at,counts[kind] if kind!='schedule' else counts['slots']))
        db.commit()
    finally:db.close()
    temporary.replace(output)
    return {'database':str(output.resolve()),'imported_at':at,'counts':counts,'sources':{kind:str(path) for kind,path in paths.items()},'limitations':['В CSV пациентов есть только хэши; ФИО, даты рождения, полисы и участки отсутствуют.','CSV звонков не содержит расшифровок и маршрутов разговора.','Расписание является файловым снимком, не синхронизацией с МИС.']}

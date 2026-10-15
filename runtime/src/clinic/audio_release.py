"""A scenario build requires a current successful audit for every static message."""
from pathlib import Path
import hashlib
import json
from .admin_audio import folder,manifest


def require_audio_audit(project,root):
    bank=folder(root);path=bank/'audit.json'
    if not path.is_file():raise ValueError('Сначала проверьте все записи: Аудиофразы → Проверить произношение')
    report=json.loads(path.read_text())
    if report.get('status')!='passed':raise ValueError('Аудиобанк не прошёл проверку. Откройте отчёт произношения')
    current=bank/'current.json'
    if not current.is_file() or json.loads(current.read_text()).get('voice')!=report.get('voice'):
        raise ValueError('Модель или обработка звука изменились после проверки')
    records=manifest(root);checked={p['key']:p for p in report.get('phrases',[]) if p.get('status')=='passed'}
    for flow in project['flows']:
        for state in flow['states']:
            for phrase in state['statements']:
                if phrase['kind']!='say' or '{' in phrase['text']:continue
                rate=float(phrase.get('args',{}).get('rate',flow['rate']))
                candidates=[(k,e) for k,e in records.items() if e['text']==phrase['text'] and float(e['rate'])==rate and e.get('voice')==report.get('voice')]
                if not candidates:raise ValueError('Новая или изменённая реплика ещё не записана и не проверена: '+phrase['text'][:80])
                for key,entry in candidates:
                    check=checked.get(key)
                    if not check or check.get('text')!=phrase['text'] or float(check.get('rate',0))!=rate:continue
                    file=bank/(key+'.wav')
                    if file.is_file() and hashlib.file_digest(file.open('rb'),'sha256').hexdigest()==check.get('file_sha256'):break
                else:raise ValueError('Запись изменена после проверки: '+phrase['text'][:80])
    return {'voice':report['voice'],'count':report['count'],'audit_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

"""Adjacent dialogue steps for registrar forms; the administrative graph stays separate."""
from .admin_rpc import module_call

INTENT_LABELS={'book':'Записаться на приём','reschedule':'Перенести запись','cancel':'Отменить запись','confirm':'Подтвердить приём','my_booking':'Узнать свою запись','operator':'Поговорить с человеком','info_schedule':'Узнать часы работы','info_holidays':'Узнать праздничные дни','info_address':'Узнать адрес','info_docs':'Узнать документы','referral':'Уточнить направление','emergency':'Срочное обращение','medical':'Врачебный вопрос','complaint':'Разобрать жалобу'}
EDGE_LABELS={'ok':'Получилось','error':'Нужна помощь','empty':'Не нашлось','many':'Несколько вариантов','yes':'Да','no':'Нет','silence':'Человек молчит','any':'Другой ответ','default':'Другой вариант','date':'Назван день','time':'Названо время','daypart':'Утром или вечером','timeout':'Нет ответа','retry':'Повторная попытка'}


def target(flow,value):
    return value if '.' in value else flow+'.'+value


def neighbor_view(project, state=None):
    index={f['name']+'.'+s['name']:(f,s) for f in project['flows'] for s in f['states']}
    if not state:
        flow=next((f for f in project['flows'] if f['name']=='inbound'),project['flows'][0]);state=flow['name']+'.'+flow['entry']
    if state not in index:raise ValueError('Шаг не найден в установленном сценарии')
    def card(key):
        flow,node=index[key]
        texts=[a['text'] for a in node['statements'] if a['kind']=='say']
        titles={'welcome':'Начало звонка','listen_free':'Человек рассказывает о запросе','operator':'Передаём разговор регистратору','intent_book':'Начинаем запись','intent_existing':'Работаем с текущей записью','info_say':'Отвечаем на вопрос','goodbye':'Прощаемся'}
        operations={'lookup_phone':'Ищем знакомый номер','find_patient':'Проверяем карточку','clinic_info':'Смотрим часы работы','holidays':'Смотрим праздничные дни','book':'Оформляем запись','reschedule':'Меняем время приёма','offer':'Подбираем время'}
        action=next((a for a in node['statements'] if a['kind']=='call'),None)
        title=node.get('label') or titles.get(node['name']) or (texts[0][:85] if texts else operations.get(action['args']['op'],'Проверяем нужные данные') if action else 'Продолжаем разговор')
        return {'id':key,'title':title,'lines':texts,'flow':flow.get('label') or flow['name']}
    def connections(key):
        flow,node=index[key];result=[]
        for edge in node['edges']:
            dest=target(flow['name'],edge['target'])
            if dest not in index:continue
            label=EDGE_LABELS.get(edge['kind'],edge['kind'])
            if edge['kind']=='dtmf':label='Клавиша '+' / '.join(edge['values'])
            if edge['kind']=='intent':label=' / '.join(INTENT_LABELS.get(v,'Особый случай') for v in edge['values'])
            if edge['kind']=='case':label='Условие: '+' / '.join(edge['values'])
            result.append({'label':label,'card':card(dest)})
        for action in node['statements']:
            if action['kind'] in {'goto','enter'}:
                dest=target(flow['name'],action['args']['target'])
                if dest in index:result.append({'label':'Продолжить','card':card(dest)})
        return result
    parents=[]
    for key in index:
        if any(c['card']['id']==state for c in connections(key)):parents.append(card(key))
    return {'current':card(state),'parents':parents,'next':connections(state),'intent_options':INTENT_LABELS,'revision':project['revision']}


def request(repo, route, post, data, query, identity, store=None):
    registrar=identity.get('registrar') if identity['role']=='registrar' else (data.get('registrar') or query.get('registrar',['100'])[0])
    if route=='/api/workshop/preview' and post:
        if identity['role']=='observer':raise ValueError('Наблюдатель может слушать готовые записи')
        import re
        text=str(data.get('text','')).strip()
        if not text or len(text)>1500:raise ValueError('Введите фразу до 1500 символов')
        # A preview does not expose a real patient's identity or claim a database write.
        text=re.sub(r'\{[^{}]+\}', 'данные из записи', text)
        rate=float(data.get('rate',1.5))
        if not 1.3<=rate<=1.75:raise ValueError('Недопустимая скорость речи')
        return module_call('tts','preview',{'text':text,'rate':rate})
    if route=='/api/workshop/step'  and not post:
        return neighbor_view(repo.read(),query.get('state',[None])[0])
    if route=='/api/workshop/calls' and not post:
        if store is not None:
            from .call_archive import CallArchive
            return CallArchive(store,module_call,repo.read()).list(registrar,identity)
        return module_call('crm','learning.calls',{'registrar':registrar})
    if route=='/api/workshop/call' and not post:
        if store is not None:
            from .call_archive import CallArchive
            return CallArchive(store,module_call,repo.read()).detail(query.get('id',[''])[0],registrar,identity,query.get('source',[None])[0])
        result=module_call('crm','learning.call',{'registrar':registrar,'call_id':query.get('id',[''])[0]})
        if not result:raise ValueError('Звонок не найден на вашем номере')
        return result
    if route=='/api/workshop/cases' and not post:
        return module_call('crm','learning.cases',{'registrar':registrar})
    if route=='/api/workshop/propose' and post:
        # The identity wins over any browser-supplied number.
        return module_call('crm','learning.propose',{**data,'registrar':registrar})
    if identity['role']=='admin' and route=='/api/workshop/night' and post:
        return module_call('crm','learning.night',{})
    raise ValueError('Неизвестное действие мастерской')


def recorded_voice(repo,state,index):
    from .admin_audio import active_manifest,recording
    project=repo.read()
    flow_name,_,name=state.partition('.')
    node=next((s for f in project['flows'] if f['name']==flow_name for s in f['states'] if s['name']==name),None)
    if node is None:raise ValueError('Реплика не найдена')
    phrases=[s['text'] for s in node['statements'] if s['kind']=='say']
    if index<0 or index>=len(phrases):raise ValueError('Реплика не найдена')
    key=next((key for key,item in active_manifest(repo.root).items() if item['text']==phrases[index]),None)
    if not key:raise ValueError('Для этой реплики нужна запись с данными конкретного звонка')
    return recording(repo.root,key)

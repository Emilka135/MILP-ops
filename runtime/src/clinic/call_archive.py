"""A common, line-scoped reader for runtime, text-test and imported call archives."""
import copy
from datetime import datetime,timezone
from .learning import number


def project_graph(project):
    nodes={};branches={}
    for flow in project['flows']:
        for node in flow['states']:
            ref=flow['name']+'.'+node['name']
            lines=[s['text'] for s in node['statements'] if s['kind']=='say']
            nodes[ref]={'id':ref,'title':node.get('label') or (lines[0][:85] if lines else ref),'lines':lines,'flow':flow.get('label') or flow['name']}
            targets=[e['target'] for e in node['edges']]+[s['args']['target'] for s in node['statements'] if s['kind'] in {'goto','enter'}]
            branches[ref]=list(dict.fromkeys(t if '.' in t else flow['name']+'.'+t for t in targets))
    return {'nodes':nodes,'titles':{key:n['title'] for key,n in nodes.items()},'branches':branches,'version':project['revision']}


def review_document(document, project, source):
    result=copy.deepcopy(document)
    transcript=result.get('transcript') or []
    trace=list(result.get('trace') or [])
    if not trace:
        for turn in transcript:
            ref=turn.get('state')
            if ref and (not trace or trace[-1]!=ref):trace.append(ref)
    saved=result.get('graph') or {}
    historical=bool(saved.get('nodes')) or source=='local'
    graph=copy.deepcopy(saved) if historical else project_graph(project)
    graph.setdefault('nodes',{});graph.setdefault('titles',{});graph.setdefault('branches',{})
    # Old records may refer to states deleted by later scenario versions. The
    # recorded path and utterances remain reviewable even without that version.
    for ref in set(trace):
        short=ref.removeprefix('demo.')
        title=graph['titles'].get(ref) or graph['titles'].get(short)
        lines=[t['text'] for t in transcript if t.get('state')==ref and t.get('role')=='assistant']
        title=title or (lines[0][:85] if lines else ref)
        if ref not in graph['nodes']:
            graph['nodes'][ref]={'id':ref,'title':title,'lines':lines,'flow':ref.partition('.')[0],'historical_only':True}
        elif not historical and lines:
            graph['nodes'][ref]['lines']=lines
        graph['titles'][ref]=title
        if ref not in graph['branches']:
            graph['branches'][ref]=[b if '.' in b else 'demo.'+b for b in graph['branches'].get(short,[])]
    # A versionless record cannot attest to branches which were not taken.
    if not historical:
        graph['branches']={ref:list(dict.fromkeys(trace[i+1] for i,value in enumerate(trace[:-1]) if value==ref)) for ref in set(trace)}
        graph['nodes']={ref:graph['nodes'][ref] for ref in set(trace)}
        graph['version']=None
    cursor=0
    for turn in transcript:
        if isinstance(turn.get('step_index'),int) and 0<=turn['step_index']<len(trace):continue
        ref=turn.get('state')
        if ref in trace[cursor:]:cursor=trace.index(ref,cursor)
        elif ref in trace:cursor=trace.index(ref)
        else:continue
        turn['step_index']=cursor
    result.update(call_id=result.get('call_id') or result.get('id'),trace=trace,transcript=transcript,graph=graph,archive_source=source,
                  can_propose=source=='runtime',historical_graph_available=historical)
    if not historical:result['archive_note']='Версия сценария для этого старого звонка не сохранена. Показаны записанные реплики и фактические переходы; непосещённые ветки восстановить нельзя.'
    return result


class CallArchive:
    def __init__(self,store,rpc,project):self.store,self.rpc,self.project=store,rpc,project
    @staticmethod
    def allowed(row,registrar,identity):
        if row['registrar']!=registrar:return False
        if identity['role']=='observer':return row['owner']==identity.get('_client') and row['call_kind']!='imported'
        return True
    def list(self,registrar,identity):
        registrar=number(registrar);warnings=[]
        try:runtime=self.rpc('crm','learning.calls',{'registrar':registrar}).get('calls',[])
        except ValueError:runtime=[];warnings.append('Телефонный архив временно недоступен. Сохранённые тестовые разговоры доступны.')
        calls=[{**row,'archive_source':'runtime','kind':'runtime','updated_at':row.get('updated_at',row['created_at'])} for row in runtime]
        for row in self.store.list_calls(registrar=registrar,metadata=True):
            if self.allowed(row,registrar,identity):calls.append({k:v for k,v in row.items() if k!='owner'}|{'archive_source':'local'})
        def timestamp(row):
            try:
                at=datetime.fromisoformat(row.get('updated_at') or row.get('created_at') or '')
                return at.replace(tzinfo=at.tzinfo or timezone.utc).timestamp()
            except (TypeError,ValueError):return 0
        calls.sort(key=timestamp,reverse=True)
        return {'calls':calls[:200],'warnings':warnings}
    def detail(self,key,registrar,identity,source=None):
        registrar=number(registrar)
        if source not in {None,'','local','runtime'}:raise ValueError('Неизвестный архив звонков')
        if source!='runtime':
            row=self.store.call_metadata(key)
            if row:
                if not self.allowed(row,registrar,identity):raise ValueError('Звонок не найден на вашем номере')
                return review_document(self.store.call(key),self.project,'local')
            if source=='local':raise ValueError('Звонок не найден на вашем номере')
        result=self.rpc('crm','learning.call',{'registrar':registrar,'call_id':key})
        if not result:raise ValueError('Звонок не найден на вашем номере')
        return review_document(result,self.project,'runtime')

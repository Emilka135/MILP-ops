"""Archived conversations stay readable independently of the current DSL."""
import pytest
from clinic.call_archive import CallArchive,review_document
from clinic.sandbox_store import SandboxStore

PROJECT={'revision':'new','flows':[]}
ADMIN={'role':'admin'}

def test_removed_state_is_reviewable_without_inventing_alternate_history():
    raw={'call_id':'old','trace':['removed.start','removed.end'],'transcript':[{'role':'assistant','text':'Настоящая старая реплика','state':'removed.start'}]}
    doc=review_document(raw,PROJECT,'runtime')
    assert doc['graph']['nodes']['removed.start']['lines']==['Настоящая старая реплика']
    assert doc['graph']['branches']=={'removed.start':['removed.end'],'removed.end':[]}
    assert not doc['historical_graph_available'] and doc['archive_note']
    assert 'graph' not in raw


def test_saved_graph_and_repeated_occurrence_are_preserved():
    raw={'call_id':'saved','trace':['x.a','x.b','x.a'],'graph':{'nodes':{'x.a':{'id':'x.a','lines':['Historical']}},'version':'old','branches':{'x.a':['x.b','x.unvisited']}},'transcript':[{'state':'x.a','text':'second visit','step_index':2}]}
    doc=review_document(raw,PROJECT,'runtime')
    assert doc['graph']['version']=='old' and doc['historical_graph_available']
    assert doc['graph']['branches']['x.a']==['x.b','x.unvisited']
    assert doc['transcript'][0]['step_index']==2


def test_local_text_calls_merge_and_sort_and_survive_runtime_failure(tmp_path):
    store=SandboxStore(tmp_path)
    store.save_call('typed','owner',{'call_id':'typed','registrar':'100','trace':['demo.menu'],'transcript':[{'role':'patient','source':'typed_test','text':'Привет','state':'demo.menu'}]})
    def rpc(module,op,args):return {'calls':[{'call_id':'old','created_at':'2000-01-01T12:00:00+03:00'}]}
    archive=CallArchive(store,rpc,PROJECT)
    rows=archive.list('100',ADMIN)['calls']
    assert [r['call_id'] for r in rows]==['typed','old'] and rows[0]['kind']=='text'
    assert archive.detail('typed','100',ADMIN,'local')['transcript'][0]['text']=='Привет'
    assert not archive.list('101',ADMIN)['calls'][0]['call_id']=='typed'
    archive.rpc=lambda *a:(_ for _ in ()).throw(ValueError('unavailable'))
    assert archive.list('100',ADMIN)['calls'][0]['call_id']=='typed'
    assert archive.list('100',ADMIN)['warnings']
    store.db.close()


def test_owner_line_and_import_access(tmp_path):
    store=SandboxStore(tmp_path)
    for key,analysis in [('own',False),('imported',True)]:store.save_call(key,'browser',{'call_id':key,'registrar':'100','analysis':analysis})
    archive=CallArchive(store,lambda *a:{'calls':[]},PROJECT)
    observer={'role':'observer','_client':'browser'}
    assert [r['call_id'] for r in archive.list('100',observer)['calls']]==['own']
    for key,line,identity in [('own','101',ADMIN),('own','100',{'role':'observer','_client':'other'}),('imported','100',observer)]:
        with pytest.raises(ValueError):archive.detail(key,line,identity,'local')
    assert archive.detail('imported','100',ADMIN,'local')['analysis']

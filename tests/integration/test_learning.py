from datetime import datetime, timedelta, timezone
import pytest
from calls.crm.registry import MockRegistry
from clinic.learning import Learning

AT=datetime(2026,10,15,23,tzinfo=timezone(timedelta(hours=4)))


@pytest.fixture
def learning(tmp_path):
    reg=MockRegistry(tmp_path/'clinic.sqlite')
    store=Learning(reg)
    store.handle('learning.archive',{'call_id':'call-x','registrar':'101','finished':True,'outcome':'done','duration_s':48,'trace':['inbound.welcome','inbound.listen_free','inbound.operator'],'transcript':[{'role':'patient','text':'перенести запись 12345678901','state':'inbound.listen_free'}]},AT)
    return store


def proposal(learning, **overrides):
    return learning.handle('learning.propose',{'registrar':'101','call_id':'call-x','source_state':'inbound.listen_free','title':'Перенос на утро','intent':'reschedule','examples':['Мне бы передвинуть приём','Поменять время записи'],'response':'Подберём другое время.',**overrides},AT)


def test_call_and_examples_are_scoped_to_registrar_and_actual_trace(learning):
    assert learning.handle('learning.calls',{'registrar':'102'},AT)[1]['calls']==[]
    assert learning.handle('learning.call',{'registrar':'102','call_id':'call-x'},AT)[0]=='empty'
    assert proposal(learning,registrar='102')[0]=='error'
    assert proposal(learning,source_state='inbound.not_in_call')[0]=='error'
    text=learning.handle('learning.call',{'registrar':'101','call_id':'call-x'},AT)[1]['transcript'][0]['text']
    assert '12345678901' not in text
    assert proposal(learning)[0]=='ok'
    info=learning.handle('learning.cases',{'registrar':'101'},AT)[1]
    assert info['labelled_examples']==2 and info['badges']['first_example']
    assert not info['badges']['in_use']


def test_night_review_tomorrow_expiry_and_phone_scope(learning):
    _,case=proposal(learning)
    assert learning.handle('learning.night',{},AT,closed=False)[0]=='blocked'
    assert learning.handle('learning.review',{'id':case['case_id'],'approve':True},AT)[0]=='blocked'
    assert learning.handle('learning.night',{},AT,closed=True)[1]['processed']==1
    assert learning.handle('learning.review',{'id':case['case_id'],'approve':True},AT)[0]=='blocked'
    # An arbitrary revision string cannot activate a response or reorder a live call.
    assert learning.handle('learning.review',{'id':case['case_id'],'approve':True,'validated_revision':'test-built-revision'},AT)[0]=='blocked'
    # Fixture for the future deployment policy; not evidence of a deployed profile.
    learning.reg._x("UPDATE learning_cases SET status='approved' WHERE id=?",(case['case_id'],))
    args={'registrar':'101','state':'inbound.listen_free','text':'Мне бы передвинуть приём!'}
    assert learning.handle('learning.alias',args,AT)[0]=='empty'
    tomorrow=AT+timedelta(days=1)
    assert learning.handle('learning.alias',{**args,'registrar':'102'},tomorrow)[0]=='empty'
    assert learning.handle('learning.alias',{**args,'state':'inbound.welcome'},tomorrow)[0]=='empty'
    assert learning.handle('learning.alias',args,tomorrow)[1]['intent']=='reschedule'
    assert learning.handle('learning.alias',args,tomorrow+timedelta(days=8))[0]=='empty'


def test_complaint_suspends_experiment(learning):
    _,case=proposal(learning)
    learning.handle('learning.night',{},AT,True)
    # Simulate the future deployment controller only to test suspension policy.
    learning.reg._x("UPDATE learning_cases SET status='approved' WHERE id=?",(case['case_id'],))
    learning.reg._conn.execute('CREATE TABLE complaints(id INTEGER,registrar TEXT,state TEXT)')
    learning.reg._x("INSERT INTO complaints VALUES(1,'101','open')")
    assert learning.handle('learning.alias',{'registrar':'101','state':'inbound.listen_free','text':'Мне бы передвинуть приём'},AT+timedelta(days=1))[0]=='empty'
    assert learning.handle('learning.cases',{'registrar':'101'},AT)[1]['cases'][0]['status']=='suspended'


def test_call_identity_cannot_be_reassigned_after_restart(learning):
    status,_=learning.handle('learning.archive',{'call_id':'call-x','registrar':'102'},AT)
    assert status=='error'
    assert learning.handle('learning.call',{'registrar':'101','call_id':'call-x'},AT)[0]=='ok'


def test_night_worker_uses_same_crm_hours_and_only_prepares(learning):
    from clinic.nightly import run_once
    _,case=proposal(learning)
    class Proxy:
        def call(self,op,args):return learning.handle(op,args,AT,closed=True)
    status,result=run_once(Proxy())
    assert status=='ok' and result['processed']==1
    row=learning.handle('learning.cases',{'registrar':'101'},AT)[1]['cases'][0]
    assert row['status']=='prepared' and not row['report']['neural_training']
    assert learning.handle('learning.review',{'id':case['case_id'],'approve':True,'validated_revision':'arbitrary'},AT)[0]=='blocked'

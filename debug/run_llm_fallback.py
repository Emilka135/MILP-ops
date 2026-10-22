"""Actual Rust FSM + deployed NLU/LLM API; all MIS operations use fresh memory SQLite.

Run manually after starting the local LLM profile. This is not an Actions runner:
Actions forbids generative model calls and exercises its isolated contracts instead.
"""
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import date, datetime

from calls.crm.registry import MockRegistry, seed_demo
from clinic.crm import ClinicCrm

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'debug/runs/llm-fallback'
REMOTE='''import json,sys
from calls.services.netutil import post_json
r=json.load(sys.stdin)
print(json.dumps(post_json('http://127.0.0.1:8080/v1/'+r['op'],r['args'],timeout=20),ensure_ascii=False))'''


def nlu(op,args):
    p=subprocess.run(['docker','exec','-i','clinic-voice-nlu-1','python','-c',REMOTE],
                     input=json.dumps({'op':op,'args':args}),text=True,capture_output=True,timeout=25,check=True)
    r=json.loads(p.stdout)
    return r['status'],r['payload']


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    os.environ['CRM_NOW']='2026-10-23T11:00:00+04:00'
    registry=MockRegistry(':memory:',now=datetime(2026,10,23,11))
    seed_demo(registry,date(2026,10,24))
    crm=ClinicCrm(registry)
    events=[];calls=[];history=[];frames=[]
    def replay():
        p=subprocess.run([str(ROOT/'target/debug/contract-replay')],input=json.dumps({
            'flow':'inbound','vars':{'today':'2026-10-23'},'events':events}),text=True,capture_output=True,timeout=10,check=True)
        return json.loads(p.stdout)['frames']
    def drain():
        nonlocal frames
        for _ in range(30):
            frame=frames[-1];history.append(frame['state'])
            pending=[a['args'] for a in frame['actions'] if a['kind']=='call']
            if not pending:return frame
            assert len(pending)==1,pending
            call=pending[0];args={**call['args'],'call_id':'synthetic-llm-fallback'}
            assert call['module'] in {'crm','nlu'},call
            # Stop before any write, even if the caller sequence were wrong.
            assert call['op'] not in {'book','cancel','reschedule','confirm'},call
            started=time.monotonic()
            status,payload=(crm.handle(call['op'],args) if call['module']=='crm' else nlu(call['op'],args))
            calls.append({**call,'status':status,'seconds':round(time.monotonic()-started,3),'payload':payload})
            events.append({'kind':'module','value':status,'payload':payload,'correlate':True})
            frames=replay()
        raise AssertionError('module loop limit')
    def say(text):
        nonlocal frames
        status,payload=nlu('interpret',{'text':text,'today':'2026-10-23','state':frames[-1]['state'],
                                      'identity_field':frames[-1].get('input_field')})
        assert status=='ok',payload
        events.append({'kind':'speech','value':text,'payload':payload});frames=replay()
        return drain()
    try:
        frames=replay()
        say('Запишите меня к терапевту после обеда и до полуночи')
        say('Петрова Мария Сергеевна')
        say('23.11.1992')
        result=say('Да, всё верно')
        assert result['state']=='time_clarification.confirm',{'state':result['state'],'history':history,'calls':calls}
        assert not registry.execute('SELECT * FROM bookings')
        result=say('Да, такое время подходит')
        assert result['state']=='inbound.book_offer',{'state':result['state'],'history':history,'calls':calls}
        spec=result['vars']['availability_constraints']
        assert spec['time_to']=='24:00' and not spec['needs_clarification']
        assert not registry.execute('SELECT * FROM bookings')
        report={'status':'passed','mis':'isolated in-memory SQLite; writes prohibited by runner',
                'llm':'deployed local API, actual model','state':result['state'],'history':history,'calls':calls,'events':events,
                'bookings':0,'constraints':spec}
        (OUT/'fsm-live-local-api.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k not in {'calls','events'}},ensure_ascii=False))
    finally:
        registry._conn.close()


if __name__=='__main__':main()

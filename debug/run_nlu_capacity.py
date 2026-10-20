"""Measure real CPU NLU; encoder throughput is independent of call capacity."""
import argparse
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'runtime/src'),str(ROOT)]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--requests',type=int,default=100)
    parser.add_argument('--max-seconds',type=float,default=60)
    parser.add_argument('--output',type=Path,default=ROOT/'debug/runs/docx-contracts/capacity/results.json')
    args=parser.parse_args()
    if args.requests<100 or not 0<args.max_seconds<=60:parser.error('Acceptance requires at least 100 requests within at most 60 seconds')
    if hasattr(os,'sched_getaffinity'):
        cpus=sorted(os.sched_getaffinity(0));os.sched_setaffinity(0,set(cpus[:2]))
    os.environ['OPENBLAS_NUM_THREADS']='2';os.environ['OMP_NUM_THREADS']='2'
    from clinic.modules import Nlu
    from clinic.scenario_catalog import catalog
    source=catalog();texts=[]
    for row in source['control_dialogues']:
        texts.extend(q for q in re.findall('«([^»]+)»',row['input']) if q!='доверенное лицо')
    for scenario in source['scenarios']:
        for item in scenario['passport'].get('Триггеры',[]):
            texts.extend(q for q in re.findall('«([^»]+)»',item['text']) if '…' not in q)
    load_started=time.perf_counter();nlu=Nlu('e5');load_seconds=time.perf_counter()-load_started
    encoder=nlu.semantic.encoder
    providers=encoder.session.get_providers()
    if providers!=['CPUExecutionProvider']:raise RuntimeError('Capacity acceptance requires CPU-only inference')
    for text in texts[:5]:nlu.handle('interpret',{'text':text,'today':'2026-10-15'})
    latencies=[];start=time.perf_counter();completed=0
    for i in range(args.requests):
        before=time.perf_counter()
        status,result=nlu.handle('interpret',{'text':texts[i%len(texts)],'today':'2026-10-15'})
        if status!='ok' or result['semantic']['engine']!=encoder.name:raise RuntimeError('Real encoder request failed')
        latencies.append(time.perf_counter()-before);completed+=1
        if time.perf_counter()-start>args.max_seconds:break
    elapsed=time.perf_counter()-start
    passed=completed==args.requests and elapsed<=args.max_seconds
    report={'passed':passed,'engine':encoder.name,'providers':providers,'cpu_threads':encoder.session.get_session_options().intra_op_num_threads,
            'cpu_affinity_count':len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None,
            'requests':completed,'required_requests':args.requests,'elapsed_seconds':round(elapsed,6),
            'minimum_requests_per_minute':100,'requests_per_minute':round(completed/elapsed*60,2),
            'p50_ms':round(statistics.median(latencies)*1000,3),'p95_ms':round(sorted(latencies)[max(0,int(len(latencies)*.95)-1)]*1000,3),
            'load_seconds':round(load_seconds,6),'external_llm_calls':0,
            'end_to_end_calls_verified':False,'end_to_end_calls_per_minute_requirement':20}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return int(not passed)


if __name__=='__main__':raise SystemExit(main())

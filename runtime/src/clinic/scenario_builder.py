"""Optional offline scenario compiler; consumes only admin-validated snapshot jobs."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import time

from .scenario_editor import compile_project

ROOT=Path(__file__).resolve().parents[3]


def atomic(path, value):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    os.replace(temp,path)


def build(request, data, root=ROOT):
    item=json.loads(request.read_text());job=item['job'];job_id=job['id']
    if len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('invalid build identifier')
    result=data/'build-jobs'/(job_id+'.result.json')
    work=data/'build-work'/job_id;work.mkdir(parents=True,exist_ok=True)
    try:
        sources,_,errors=compile_project(item['project'])
        if errors:raise ValueError('\n'.join(errors))
        for name in ['Cargo.toml','Cargo.lock','build.rs']:
            shutil.copy2(root/name,work/name)
        for name in ['src','tools','runtime/src/calls/scenario']:
            shutil.copytree(root/name,work/name,dirs_exist_ok=True)
        target=work/'scenarios';target.mkdir(exist_ok=True)
        for name,text in sources.items():(target/name).write_text(text)
        source_hash=hashlib.sha256()
        for name in ['Cargo.toml','Cargo.lock','build.rs',*sorted(str(p.relative_to(work)) for p in (work/'src').rglob('*.rs'))]:
            source_hash.update(name.encode());source_hash.update((work/name).read_bytes())
        log_path=work/'build.log'
        with log_path.open('w') as log:
            done=subprocess.run(['cargo','build','--offline','--locked','--bin','clinic-voice'],cwd=work,
                stdout=log,stderr=subprocess.STDOUT,timeout=300,check=False)
        job['log']=log_path.read_text()[-20000:]
        if done.returncode:raise ValueError('Компилятор отклонил сборку; смотрите журнал')
        artifact=data/'artifacts'/job_id;artifact.mkdir(parents=True,exist_ok=True)
        target_dir=Path(os.environ.get('CARGO_TARGET_DIR',str(work/'target')))
        shutil.copy2(target_dir/'debug/clinic-voice',artifact/'clinic-voice')
        (artifact/'project.json').write_text(json.dumps(item['project'],ensure_ascii=False,indent=2))
        job.update(status='succeeded',artifact=job_id,engine_source_sha256=source_hash.hexdigest(),
            message='Версия собрана отдельным сборщиком. Установка выполняется при обновлении сервиса; активные звонки продолжают работать.')
    except (ValueError,OSError,subprocess.TimeoutExpired) as exc:
        job.update(status='failed',error=str(exc))
    atomic(result,job)
    request.unlink(missing_ok=True)
    # Keep compiler log and artifact, discard duplicated source snapshot after completion.
    for name in ['src','tools','runtime','scenarios']:
        shutil.rmtree(work/name,ignore_errors=True)
    return job


def main():
    data=Path(os.environ.get('EDITOR_DATA','/data'));queue=data/'build-jobs';queue.mkdir(parents=True,exist_ok=True)
    for path in queue.glob('*.running.json'):
        item=json.loads(path.read_text());job=item['job'];job.update(status='failed',error='Сборщик был перезапущен во время задания')
        atomic(queue/(job['id']+'.result.json'),job);path.unlink()
    while True:
        for request in sorted(queue.glob('*.request.json')):
            running=request.with_name(request.name.replace('.request.json','.running.json'))
            os.replace(request,running)
            build(running,data)
        time.sleep(2)


if __name__=='__main__':main()

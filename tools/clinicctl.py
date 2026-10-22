"""Simple clinic start/stop/status with actual published URLs and an honest backup placeholder."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
GPU_ENABLED=False
DIALOGUE_ENABLED=False
TARGETS={'all':['orchestrator','scenario-editor'],'editor':['scenario-editor'],'voice':['orchestrator']}


def docker(arguments,capture=False):
    files=[]
    if GPU_ENABLED or DIALOGUE_ENABLED:
        files=['-f','compose.yaml']
        if (ROOT/'compose.override.yaml').exists():files+=['-f','compose.override.yaml']
        if GPU_ENABLED:files+=['-f','compose.gpu.yaml']
        if DIALOGUE_ENABLED:files+=['-f','compose.dialogue.yaml']
    return subprocess.run(['docker','compose',*files,*arguments],cwd=ROOT,text=True,
        stdout=subprocess.PIPE if capture else None,stderr=subprocess.PIPE if capture else None,check=False,
        env={**os.environ,'CLINIC_LOCAL_UID':os.environ.get('CLINIC_LOCAL_UID',str(getattr(os,'getuid',lambda:1000)())),
             'CLINIC_LOCAL_GID':os.environ.get('CLINIC_LOCAL_GID',str(getattr(os,'getgid',lambda:1000)()))})


def address(service,port):
    result=docker(['port',service,str(port)],capture=True)
    if result.returncode:raise RuntimeError('Не удалось узнать адрес сервиса '+service)
    lines=result.stdout.strip().splitlines()
    if not lines:raise RuntimeError("Сервис не сообщил опубликованный порт: "+service)
    line=lines[0]
    host,number=line.rsplit(':',1)
    if host.strip('[]') in {'0.0.0.0','127.0.0.1','::','::1'}:host='localhost'
    return f'http://{host}:{int(number)}'


def show_access(target):
    print('\nГОТОВО. Адреса на этом компьютере:')
    if target in {'all','editor'}:
        url=address('scenario-editor',8090)
        print('  Администратору:  '+url)
        print('  Регистратору:    '+url+'/registrar')
    if target in {'all','voice'}:
        url=address('orchestrator',8080)
        print('  Голосовой API:   '+url)
        print('  Окно оператора:  '+url+'/operator')
    print('\nОстановить с бэкапом:')
    print('  ./clinic stop'+(' '+target if target!='all' else '')+' --backup'+(' --gpu' if GPU_ENABLED else '')+(' --dialogue' if DIALOGUE_ENABLED else ''))
    print('  Бэкап пока заглушка: резервная копия ещё не создаётся. Остановка сохраняет данные.')


def backup_placeholder(target):
    report={'created':False,'status':'not_implemented','target':target,
        'at':datetime.now(timezone.utc).isoformat(),'data_volumes_deleted':False}
    path=ROOT/'debug/runs/control/backup-placeholder.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('БЭКАП: пока заглушка — резервная копия не создана.')


def main():
    global GPU_ENABLED,DIALOGUE_ENABLED
    parser=argparse.ArgumentParser(description='Запуск и остановка регистратуры')
    parser.add_argument('command',choices=['start','stop','status'])
    parser.add_argument('target',nargs='?',choices=TARGETS,default='all')
    parser.add_argument('--backup',action='store_true',help='пока заглушка; данные при остановке сохраняются')
    parser.add_argument('--no-build',action='store_true',help='запустить уже собранную версию')
    parser.add_argument('--gpu',action='store_true',help='локальный ASR на NVIDIA GPU (Compose CUDA overlay)')
    parser.add_argument('--dialogue',action='store_true',help='локальная LLM сложных уточнений с прогревом до запуска звонков')
    args=parser.parse_args()
    GPU_ENABLED=args.gpu
    DIALOGUE_ENABLED=args.dialogue
    if args.command=='start':
        from check_space import require_space
        require_space()
        (ROOT/'secrets').mkdir(mode=0o700,exist_ok=True)
        command=['up','-d','--no-build' if args.no_build else '--build',*TARGETS[args.target]]
        result=docker(command)
        if result.returncode:
            print('\nНе удалось запустить. Журнал: docker compose logs --tail=80',file=sys.stderr)
            return result.returncode
        # Expected-successful one-shot dependencies ran in phase one. Wait
        # only for persistent endpoints so Compose does not require them running.
        result=docker(['up','-d','--no-deps','--wait','--wait-timeout','240','--no-build',*TARGETS[args.target]])
        if result.returncode:
            print('Сервис ещё не готов. Журнал: docker compose logs --tail=80',file=sys.stderr)
            return result.returncode
        show_access(args.target)
    elif args.command=='stop':
        if args.backup:backup_placeholder(args.target)
        services=[] if args.target=='all' else TARGETS[args.target]
        result=docker(['stop',*services])
        if result.returncode:return result.returncode
        print('Остановлено. Данные сохранены. Запустить снова: ./clinic start'+(' '+args.target if args.target!='all' else ''))
    else:
        result=docker(['ps'])
        if result.returncode:return result.returncode
        running=docker(['ps','--status','running','--services'],capture=True).stdout.split()
        if all(service in running for service in TARGETS[args.target]):show_access(args.target)
        else:print('Часть сервисов остановлена. Запуск: ./clinic start'+(' '+args.target if args.target!='all' else ''))
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (OSError,ValueError,RuntimeError) as error:sys.exit(str(error))

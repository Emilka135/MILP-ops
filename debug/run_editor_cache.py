"""Measure two warm builds and starts; no pruning and no deletion of shared caches."""
import argparse
import json
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--override');args=parser.parse_args()
    out=ROOT/'debug/runs/editor-cache';out.mkdir(parents=True,exist_ok=True)
    compose=['docker','compose','-f','compose.yaml']
    if args.override:compose+=['-f',args.override]
    results=[]
    for repeat in range(1,3):
        for stage,command in [('build',compose+['build','scenario-editor']),('start',compose+['up','-d','--no-build','--wait','scenario-editor'])]:
            at=time.monotonic()
            with (out/f'{repeat}-{stage}.log').open('w') as log:
                process=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=180)
            row={'repeat':repeat,'stage':stage,'seconds':round(time.monotonic()-at,3),'exit_code':process.returncode}
            results.append(row)
            if process.returncode:raise RuntimeError(row)
    images=json.loads(subprocess.check_output(['docker','image','inspect','clinic-voice-scenario-editor']))
    report={'status':'passed','runs':results,'editor_image_bytes':images[0]['Size'],'rust_in_admin_image':False}
    (out/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()

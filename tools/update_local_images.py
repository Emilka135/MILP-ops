"""Rebuild source layers over the project's own existing runtime images offline."""
import argparse
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='debug/runs/local-images');args=parser.parse_args()
    evidence={}
    for module in ('crm','nlu','agent','sms','operator','tts','asr','scenario-editor','scenario-builder','orchestrator'):
        if module=='orchestrator':
            subprocess.run(['cargo','build','--locked','--offline','--release','--bin','clinic-voice'],cwd=ROOT,check=True)
        name='clinic-voice-'+('asr-gpu' if module=='asr' else module)
        # Freeze the current local ID before replacing its friendly tag.
        before=subprocess.check_output(['docker','image','inspect',name,'--format','{{.Id}}'],text=True).strip()
        base_tag='clinic-voice-local-base:'+module
        subprocess.run(['docker','tag',before,base_tag],check=True)
        dockerfile={'scenario-editor':'docker/local-editor-update.Dockerfile','scenario-builder':'docker/local-builder-update.Dockerfile','tts':'docker/local-tts-update.Dockerfile','orchestrator':'docker/local-orchestrator-update.Dockerfile'}.get(module,'docker/local-update.Dockerfile')
        subprocess.run(['docker','build','--network=none','--pull=false','--build-arg','BASE_IMAGE='+base_tag,
                        '--build-arg','SOURCE_ROOT='+('/workspace' if module in {'scenario-editor','scenario-builder'} else '/app'),
                        '-f',dockerfile,'-t',name,'.'],cwd=ROOT,check=True)
        after=subprocess.check_output(['docker','image','inspect',name,'--format','{{.Id}}'],text=True).strip()
        evidence[module]={'dependency_runtime_image':before,'updated_image':after}
    out=ROOT/args.output
    out.mkdir(parents=True,exist_ok=True)
    (out/'local-images.json').write_text(json.dumps(evidence,indent=2))

if __name__=='__main__':main()

"""Rebuild source layers over the project's own existing runtime images offline."""
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    evidence={}
    for module in ('crm','nlu','agent','sms','operator','tts','asr','scenario-editor'):
        name='clinic-voice-'+('asr-gpu' if module=='asr' else module)
        # Freeze the current local ID before replacing its friendly tag.
        before=subprocess.check_output(['docker','image','inspect',name,'--format','{{.Id}}'],text=True).strip()
        base_tag='clinic-voice-local-base:'+module
        subprocess.run(['docker','tag',before,base_tag],check=True)
        subprocess.run(['docker','build','--network=none','--pull=false','--build-arg','BASE_IMAGE='+base_tag,
                        '--build-arg','SOURCE_ROOT='+('/workspace' if module=='scenario-editor' else '/app'),
                        '-f','docker/local-update.Dockerfile','-t',name,'.'],cwd=ROOT,check=True)
        after=subprocess.check_output(['docker','image','inspect',name,'--format','{{.Id}}'],text=True).strip()
        evidence[module]={'dependency_runtime_image':before,'updated_image':after}
    out=ROOT/'debug/runs/admin-scenarios'
    out.mkdir(parents=True,exist_ok=True)
    (out/'local-images.json').write_text(json.dumps(evidence,indent=2))

if __name__=='__main__':main()

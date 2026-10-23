"""Build Rust offline with the project's existing compiler image and named cache."""
import argparse,json,hashlib,subprocess,tempfile
from pathlib import Path
from check_space import require_space

ROOT=Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser();p.add_argument('--project',default='clinic-voice');p.add_argument('--output',type=Path,default=ROOT/'debug/runs/local-runtime');args=p.parse_args()
 require_space(ROOT,512*1024*1024);args.output.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='clinic-rust-runtime-') as directory:
  context=Path(directory)
  command=['docker','run','--rm','--network','none','--mount',f'type=bind,src={ROOT},dst=/source,readonly','--mount',f'type=volume,src={args.project}_editor-cargo,dst=/cargo-target','--mount',f'type=bind,src={context},dst=/output','-e','CARGO_TARGET_DIR=/cargo-target/dialogue-runtime','-w','/source','--entrypoint','sh','clinic-voice-scenario-builder','-c','cargo build --offline --locked --release --bin clinic-voice && cp /cargo-target/dialogue-runtime/release/clinic-voice /output/clinic-voice']
  with (args.output/'rust-container-build.log').open('w') as log:subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
  digest=hashlib.sha256((context/'clinic-voice').read_bytes()).hexdigest()
  with (args.output/'orchestrator-overlay.log').open('w') as log:subprocess.run(['docker','build','--network=none','--pull=false','-f',str(ROOT/'docker/local-orchestrator.Dockerfile'),'-t','clinic-voice-orchestrator',str(context)],stdout=log,stderr=subprocess.STDOUT,check=True)
  (args.output/'binary.json').write_text(json.dumps({'binary_sha256':digest,'compiler_image':'clinic-voice-scenario-builder','cache_volume':args.project+'_editor-cargo','network':'none'},indent=2))
 require_space()
if __name__=='__main__':main()

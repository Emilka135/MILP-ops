import json
import shutil
from pathlib import Path
from clinic.scenario_editor import Repository
from clinic.scenario_builder import build

ROOT=Path(__file__).resolve().parents[2]


def test_queue_compiles_a_validated_snapshot_and_publishes_artifact(tmp_path, monkeypatch):
    # Audio certificate policy is tested separately; this test compiles a real Rust artifact.
    monkeypatch.setattr('clinic.audio_release.require_audio_audit',lambda *_:{'scope':'compiler_dependency_fixture'})
    # Use actual cargo with cached dependencies, no fake success or executable.
    shutil.copytree(ROOT/'scenarios', tmp_path/'scenarios')
    monkeypatch.setenv('EDITOR_BUILD_QUEUE','1')
    monkeypatch.setenv('CARGO_TARGET_DIR',str(ROOT/'target'))
    repo=Repository(tmp_path,tmp_path/'data',allow_build=True)
    before=repo.read()
    job=repo.start_build(before['revision'])
    assert job['queued'] and job['status']=='running'
    path=repo.data/'build-jobs'/(job['id']+'.request.json')
    result=build(path,repo.data,ROOT)
    assert result['status']=='succeeded',result
    assert repo.build_status()['status']=='succeeded'
    artifact=repo.data/'artifacts'/job['id']
    assert (artifact/'clinic-voice').stat().st_size>100000
    assert json.loads((artifact/'project.json').read_text())['revision']==before['revision']
    assert len(result['engine_source_sha256'])==64

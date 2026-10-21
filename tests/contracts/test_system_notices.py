"""System responses are scenario data, and deletion cannot create a runtime panic."""
import importlib.util
from pathlib import Path
import shutil

import pytest

ROOT=Path(__file__).resolve().parents[2]


def compiler_for(tmp_path,monkeypatch):
    shutil.copytree(ROOT/'scenarios',tmp_path/'scenarios')
    spec=importlib.util.spec_from_file_location('system_notice_compiler',ROOT/'tools/compile_rust.py')
    compiler=importlib.util.module_from_spec(spec);spec.loader.exec_module(compiler)
    monkeypatch.setattr(compiler,'ROOT',tmp_path)
    return compiler


def test_missing_system_notice_cannot_compile(tmp_path,monkeypatch):
    compiler=compiler_for(tmp_path,monkeypatch)
    (tmp_path/'scenarios/system_notices.flow').unlink()
    with pytest.raises(ValueError,match='system_notices.emergency'):
        compiler.compile_to(tmp_path/'out')


def test_dynamic_system_notice_cannot_skip_static_audio_gate(tmp_path,monkeypatch):
    compiler=compiler_for(tmp_path,monkeypatch)
    path=tmp_path/'scenarios/system_notices.flow'
    text=path.read_text().replace('Хорошо, аудиозапись разговора отключена.','{unverified_patient}, аудиозапись отключена.')
    path.write_text(text)
    with pytest.raises(ValueError,match='system_notices.record_opt_out'):
        compiler.compile_to(tmp_path/'out')

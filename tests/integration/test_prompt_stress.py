"""Current prompt coverage and real CIS synthesis reject unresolved pronunciation."""

import json

import pytest
from calls.tts.silero import SileroTTS, build_ssml
from calls.tts.stress import DictAccentor, NoStress, require_stress_marks

from tools.check_prompt_stress import check_prompts, collect_prompts, main


def test_current_root_scenarios_demo_and_expanded_abbreviations_are_covered():
    records = collect_prompts()
    assert any(r["source"] == "demo_call" for r in records)
    assert any(r["kind"] == "template_static" for r in records)
    checked = check_prompts(records)
    assert all(r["status"] in {"passed", "missing_stress"} for r in checked), [
        r for r in checked if r["status"] != "passed"
    ]
    ultrasound = next(
        r for r in checked if r["source"] == "normalizer.abbreviation.узи"
    )
    assert ultrasound["normalized_text"] == "ультразвуковое исследование"
    assert ultrasound["stressed_text"] == "ультразвуков+ое иссл+едование"


def test_new_unknown_prompt_fails_preflight_with_actionable_words():
    checked = check_prompts(
        [{"source": "new_prompt", "text": "Нейроэнтерокибернетика"}]
    )
    assert checked[0]["status"] == "missing_stress"
    assert checked[0]["missing_words"] == ["нейроэнтерокибернетика"]
    assert "проверенное произношение" in checked[0]["error"]


def test_cli_exits_nonzero_and_preserves_failed_report(tmp_path):
    assert main(["--strict", "--text", "Нейроэнтерокибернетика", "--out", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "result.json").read_text())
    assert report["status"] == "failed"
    assert "нейроэнтерокибернетика" in report["missing_words"]


def test_real_synthesis_accepts_unknown_and_preserves_optional_stress():
    # Acoustic dependency is explicit; this checks the production boundary,
    # not naturalness. Real audio is verified separately in Docker.
    import contextlib
    import threading
    import numpy as np
    from types import SimpleNamespace
    requests=[]
    synth = SileroTTS.__new__(SileroTTS)
    synth.kind,synth.accentor,synth.speaker = 'cis',DictAccentor(),'ru_ekaterina'
    synth._lock=threading.Lock()
    synth._torch=SimpleNamespace(random=SimpleNamespace(fork_rng=lambda **kw:contextlib.nullcontext()),manual_seed=lambda x:None)
    synth.model=SimpleNamespace(apply_tts=lambda **kw: requests.append(kw) or np.sin(np.arange(48000)*.1).astype('float32')*.1)
    assert len(synth.synth_quality('Врач Шварценеггер.',1))>0
    assert 'Шварценеггер' in requests[-1]['ssml_text']
    synth.synth_quality('Врач Эрн+андес.',1)
    assert 'Эрн+андес' in requests[-1]['ssml_text']


def test_default_cli_keeps_unknown_stress_optional(tmp_path):
    assert main(['--text','Нейроэнтерокибернетика','--out',str(tmp_path)])==0
    assert not json.loads((tmp_path/'result.json').read_text())['manual_stress_required']


@pytest.mark.parametrize(
    "text", ["Эрнандес+", "Эр+нандес", "+Эрн+андес", "Эрн+андес-Нейроэнтерокибернетика"]
)
def test_invalid_or_partial_stress_marks_do_not_bypass_gate(text):
    with pytest.raises(ValueError, match="ударение"):
        build_ssml(text, 1, DictAccentor(), require_stress=True)


def test_explicit_pronunciation_is_preserved_and_allowed():
    ssml, _ = build_ssml("Врач Эрн+андес.", 1.5, DictAccentor(), require_stress=True)
    assert "Эрн+андес" in ssml


def test_fallback_cannot_claim_unmarked_unknown_is_valid():
    accentor = DictAccentor(fallback=lambda word: word)
    with pytest.raises(ValueError, match="ударение"):
        build_ssml("Нейроэнтерокибернетика", 1, accentor, require_stress=True)


def test_neutral_clitics_and_hyphen_particles_remain_neutral():
    accentor = DictAccentor()
    spoken = accentor("Что-нибудь на завтра?")
    assert spoken == "Чт+о-нибудь на з+автра?"
    assert accentor(spoken) == spoken
    require_stress_marks(spoken)


def test_turning_accentor_off_does_not_silently_disable_cis_gate():
    with pytest.raises(ValueError, match="ударение"):
        build_ssml("Поликлиника", 1, NoStress(), require_stress=True)

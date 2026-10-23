import copy
import json
from pathlib import Path
import shutil

import pytest
from clinic.scenario_editor import (
    Repository,
    compile_project,
    clean_flow,
    serialize,
    Conflict,
)
from clinic.editor_auth import EditorAuth, password_hash

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repository(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    return Repository(tmp_path, tmp_path / "data")


def test_all_flows_roundtrip_without_manual_code(repository):
    project = repository.read()
    result = repository.inspect(project)
    assert result["valid"], result["errors"]
    for f in project["flows"]:
        flow = clean_flow(serialize(f))
        assert flow["name"] == f["name"] and len(flow["states"]) == len(f["states"])
        for before,after in zip(f["states"],flow["states"]):
            assert [(s["kind"],s["args"]) for s in before["statements"]]==[(s["kind"],s["args"]) for s in after["statements"]]


@pytest.mark.parametrize('rate', [.75, 1.0, 1.3, 1.75, 2.5])
def test_frozen_corpus_speed_range_roundtrips_through_editor(repository, rate):
    project = repository.read()
    project['flows'][0]['rate'] = rate
    assert repository.inspect(project)['valid']
    stored = repository.save(project, project['revision'])
    assert stored['flows'][0]['rate'] == rate


@pytest.mark.parametrize('rate', [.74, 2.51, float('nan'), float('inf')])
def test_invalid_speech_speed_does_not_replace_project(repository, rate):
    project = repository.read()
    revision = project['revision']
    project['flows'][0]['rate'] = rate
    with pytest.raises(ValueError, match='Скорость'):
        repository.save(project, revision)
    assert repository.read()['revision'] == revision


def test_quoted_hash_inside_prompt_is_not_a_comment(repository):
    project = repository.read()
    s = next(
        s
        for f in project["flows"]
        for s in f["states"]
        if any(a["kind"] == "say" for a in s["statements"])
    )
    say = next(a for a in s["statements"] if a["kind"] == "say")
    say["text"] = 'Скажите "да" или нажмите #. Апостроф: O\'Connor.'
    assert repository.inspect(project)["valid"]
    stored = repository.save(project, project["revision"])
    assert any(
        a["text"] == say["text"]
        for f in stored["flows"]
        for s in f["states"]
        for a in s["statements"]
    )


def test_save_search_history_conflict_and_restore(repository):
    project = repository.read()
    revision = project["revision"]
    project["flows"][0]["states"][0]["label"] = "Проверка фамилии Шварценеггер"
    stored = repository.save(project, revision)
    assert repository.search("Шварц")
    with pytest.raises(Conflict):
        repository.save(project, revision)
    restored = repository.restore(revision, stored["revision"])
    assert not repository.search("Шварц")
    assert len(repository.history()) >= 2


def test_invalid_target_does_not_replace_working_project(repository):
    project = repository.read()
    revision = project["revision"]
    state = next(s for f in project["flows"] for s in f["states"] if s["edges"])
    state["edges"][0]["target"] = "not_existing_node"
    assert not repository.inspect(project)["valid"]
    with pytest.raises(ValueError):
        repository.save(project, revision)
    assert repository.read()["revision"] == revision


def test_path_injection_and_arbitrary_statement_are_rejected(repository):
    project = repository.read()
    project["flows"][0]["name"] = "../../outside"
    assert not repository.inspect(project)["valid"]
    project = repository.read()
    project["flows"][0]["states"][0]["statements"].append(
        {"kind": "exec", "args": {}, "text": "bad"}
    )
    assert not repository.inspect(project)["valid"]


def test_password_session_is_separate_from_call_api(tmp_path):
    path = tmp_path / "password.json"
    path.write_text(json.dumps(password_hash("admin")))
    auth = EditorAuth(path)
    assert not auth.allowed({"Authorization": "Bearer any-call-api-token"})
    with pytest.raises(ValueError):
        auth.login("incorrect", "local")
    cookie = auth.login("admin", "local")
    headers = {"Cookie": "clinic_admin=" + cookie}
    assert auth.allowed(headers)
    auth.logout(headers)
    assert not auth.allowed(headers)


def test_failed_install_rolls_back_all_sources(repository, monkeypatch):
    import clinic.scenario_editor as editor
    project = repository.read()
    original = project['revision']
    next(a for f in project['flows'] for s in f['states'] for a in s['statements'] if a['kind'] == 'say')['text'] = 'Новый вопрос'
    replace = editor.os.replace
    calls = 0
    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('simulated disk error')
        return replace(source, target)
    monkeypatch.setattr(editor.os, 'replace', fail_second)
    with pytest.raises(OSError):
        repository.save(project, original)
    assert repository.read()['revision'] == original


def test_login_rate_limit_and_changed_password(tmp_path):
    auth = EditorAuth(tmp_path / 'password.json')
    for _ in range(5):
        with pytest.raises(ValueError, match='Неверный'):
            auth.login('wrong', 'client')
    with pytest.raises(ValueError, match='15 минут'):
        auth.login('admin', 'client')
    (tmp_path / 'password.json').write_text(json.dumps(password_hash('new long password')))
    with pytest.raises(ValueError):
        auth.login('admin', 'other')
    cookie = auth.login('new long password', 'other')
    assert auth.allowed({'Cookie':'clinic_admin='+cookie})


def test_registrar_identity_is_bound_to_account_not_form(tmp_path):
    auth = EditorAuth(tmp_path/'password.json')
    auth.add_account('registrar_101','101','a long temporary password','Регистратор')
    token=auth.login('a long temporary password','local','registrar_101')
    identity=auth.identity({'Cookie':'clinic_admin='+token})
    assert identity['role']=='registrar' and identity['registrar']=='101'
    with pytest.raises(ValueError):auth.login('admin','other','missing_user')
    auth.add_account('registrar_101','101','another long password')
    assert not auth.allowed({'Cookie':'clinic_admin='+token})

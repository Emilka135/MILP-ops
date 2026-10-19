import subprocess
from tools import clinicctl


def test_friendly_address_uses_actual_published_port(monkeypatch):
    monkeypatch.setattr(clinicctl,'docker',lambda args,capture=False: subprocess.CompletedProcess(args,0,'127.0.0.1:18090\n',''))
    assert clinicctl.address('scenario-editor',8090)=='http://localhost:18090'


def test_stop_backup_is_honest_and_never_removes_volumes(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(clinicctl,'ROOT',tmp_path)
    monkeypatch.setattr('sys.argv',['clinic','stop','editor','--backup'])
    commands=[]
    def docker(args,capture=False):
        commands.append(args);return subprocess.CompletedProcess(args,0,'','')
    monkeypatch.setattr(clinicctl,'docker',docker)
    assert clinicctl.main()==0
    assert commands==[['stop','scenario-editor']]
    report=(tmp_path/'debug/runs/control/backup-placeholder.json').read_text()
    assert 'not_implemented' in report and '"created": false' in report
    assert 'копия не создана' in capsys.readouterr().out


def test_start_waits_only_persistent_targets_after_dependencies(monkeypatch, capsys):
    import sys
    from tools import check_space
    monkeypatch.setitem(sys.modules, 'check_space', check_space)
    monkeypatch.setattr(check_space, 'require_space', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['clinic', 'start', '--no-build'])
    commands=[]
    def docker(args, capture=False):
        commands.append(args)
        text = '127.0.0.1:8090\n' if args[0]=='port' else ''
        return subprocess.CompletedProcess(args,0,text,'')
    monkeypatch.setattr(clinicctl, 'docker', docker)
    assert clinicctl.main() == 0
    assert commands[0] == ['up','-d','--no-build','orchestrator','scenario-editor']
    assert commands[1] == ['up','-d','--no-deps','--wait','--wait-timeout','240','--no-build','orchestrator','scenario-editor']
    assert 'ГОТОВО' in capsys.readouterr().out


def test_start_never_hides_dependency_or_readiness_failure(monkeypatch, capsys):
    import sys
    from tools import check_space
    monkeypatch.setitem(sys.modules, 'check_space', check_space)
    monkeypatch.setattr(check_space, 'require_space', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['clinic', 'start', '--no-build'])
    for fail_phase in (1,2):
        commands=[]
        def docker(args, capture=False):
            commands.append(args)
            return subprocess.CompletedProcess(args,17 if len(commands)==fail_phase else 0,'','')
        monkeypatch.setattr(clinicctl, 'docker', docker)
        assert clinicctl.main() == 17
        assert len(commands) == fail_phase
        assert 'ГОТОВО' not in capsys.readouterr().out

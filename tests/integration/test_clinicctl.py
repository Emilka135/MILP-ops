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

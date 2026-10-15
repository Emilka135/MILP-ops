from collections import namedtuple
import pytest
from tools.check_space import GIB, check_space, require_space

Usage = namedtuple('Usage', 'total used free')


def test_reserves_are_strict_and_checked_before_allocation(monkeypatch, tmp_path):
    import tools.check_space as guard
    def space(path):
        return Usage(100*GIB, 0, (10 if str(path)=='/home' else 5)*GIB)
    monkeypatch.setattr(guard.shutil, 'disk_usage', space)
    assert all(not r['ok'] for r in check_space())
    with pytest.raises(RuntimeError, match='reserve'):
        require_space(tmp_path, GIB)
    monkeypatch.setattr(guard.shutil, 'disk_usage', lambda p: Usage(100*GIB,0,20*GIB))
    assert all(r['ok'] for r in require_space(tmp_path, GIB))
    with pytest.raises(RuntimeError):
        require_space(tmp_path, 21*GIB)
    with pytest.raises(ValueError):
        require_space(tmp_path, -1)

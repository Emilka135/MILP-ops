import pytest
from calls.crm.registry import MockRegistry


def test_named_and_positional_values_are_bound_not_executable(tmp_path):
    reg = MockRegistry(tmp_path / 'mirror.sqlite')
    value = "O'Connor'; DROP TABLE patients; --"
    assert reg.execute('SELECT :name AS name', {'name': value}) == [{'name': value}]
    assert reg.execute('SELECT ? AS name', (value,)) == [{'name': value}]
    assert reg.execute('SELECT COUNT(*) AS n FROM patients') == [{'n': 0}]
    assert reg.execute('SELECT 1 AS n') == [{'n': 1}]
    for invalid in (value, value.encode(), 3):
        with pytest.raises(TypeError):
            reg.execute('SELECT ? AS name', invalid)

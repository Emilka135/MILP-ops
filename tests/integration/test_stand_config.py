import pytest
from clinic.stand_config import settings


def test_demo_never_attaches_saved_live_credentials():
    assert settings({'APP_MODE':'demo','STAND_URL':'https://crm-med-mock.dbi-testing.ru/api/v1','DBI_API_TOKEN':'private'}) is None
    assert settings({'APP_MODE':'demo','ALLOW_LOCAL_STAND':'1','STAND_URL':'https://remote.example','STAND_TOKEN':'private'}) is None


def test_explicit_local_contract_stand():
    assert settings({'APP_MODE':'demo','ALLOW_LOCAL_STAND':'1','STAND_URL':'http://127.0.0.1:9999/api/v1','STAND_TOKEN':'test'}) == ('http://127.0.0.1:9999/api/v1','test')


def test_vm_token_alias_and_explicit_override():
    assert settings({'APP_MODE':'live','DBI_API_TOKEN':'vm-key'}) == ('https://crm-med-mock.dbi-testing.ru/api/v1','vm-key')
    assert settings({'APP_MODE':'live','DBI_API_TOKEN':'vm-key','STAND_TOKEN':'override'})[1] == 'override'


@pytest.mark.parametrize('env', [{'APP_MODE':'live'}, {'APP_MODE':'typo'}, {'APP_MODE':'live','STAND_URL':'http://remote','DBI_API_TOKEN':'x'}, {'APP_MODE':'live','STAND_URL':'https://user:secret@remote','DBI_API_TOKEN':'x'}])
def test_invalid_live_configuration_fails(env):
    with pytest.raises(ValueError): settings(env)

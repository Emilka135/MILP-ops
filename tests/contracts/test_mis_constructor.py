"""Offline MIS construction: transport shape never authorizes a mutation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path

import pytest

from clinic import mis_contract
from clinic.mis_contract import lookup_http, prepare

ROOT = Path(__file__).resolve().parents[2]
CLIENT = {'last_name': 'Тестов', 'first_name': 'Аркадий', 'birth_date': '19.06.1988'}
VISIT = {'client_id': 7, 'doctor_id': 3, 'scheduled_at': '24.10.2026 11:20'}


def test_required_fields_and_optional_middle_name_are_derived_from_actual_api():
    specification = json.loads((ROOT / 'docs/openapi.json').read_text())
    for identifier, schema in [('registerClient', 'ClientCreate'), ('createVisit', 'VisitCreate'), ('createEscalation', 'EscalationCreate')]:
        expected = specification['components']['schemas'][schema]['required']
        request = prepare(identifier, {})
        assert request['missing_fields'] == sorted(expected)
        assert request['complete'] is False
    request = prepare('registerClient', CLIENT)
    assert request['complete'] is True
    assert 'middle_name' not in request['body']
    assert request['authorized'] is False


def test_all_declared_operations_round_trip_through_http_lookup():
    specification = json.loads((ROOT / 'docs/openapi.json').read_text())
    seen = set()
    for path, methods in specification['paths'].items():
        for verb, operation in methods.items():
            if 'operationId' not in operation:
                continue
            identifier = operation['operationId']
            seen.add(identifier)
            body = {'registerClient': CLIENT, 'createVisit': VISIT,
                    'createEscalation': {'client_id': 7, 'description': 'Уточнить порядок приёма'},
                    'updateClient': {'comment': None}, 'updateVisit': {'status': 'Отменена'}}.get(identifier)
            params = {'id': 7} if '{id}' in path else None
            direct = prepare(identifier, body=body, path_parameters=params)
            routed = lookup_http(verb.upper(), path.replace('{id}', '7'), body)
            assert routed == direct
            assert routed['complete'] is True
            assert routed['authorized'] is False
    assert len(seen) == 12


@pytest.mark.parametrize('field,value', [('client_id', True), ('client_id', '7'), ('client_id', 0), ('client_id', 2147483648),
                                         ('doctor_id', False), ('doctor_id', 3.0), ('scheduled_at', None),
                                         ('scheduled_at', '24.10.2026 11:20:00'), ('scheduled_at', '2026-10-24T11:20'),
                                         ('scheduled_at', '31.02.2026 11:20'), ('scheduled_at', '24.10.2026 24:00')])
def test_create_visit_rejects_wire_types_invalid_dates_and_identifiers(field, value):
    request = prepare('createVisit', {**VISIT, field: value})
    assert request['invalid_fields'] == [field]
    assert not request['complete']


@pytest.mark.parametrize('date', ['31.04.1988', '29.02.1900', '19.06.88', 'завтра', ''])
def test_birth_date_is_a_complete_existing_calendar_day(date):
    request = prepare('registerClient', {**CLIENT, 'birth_date': date})
    assert request['invalid_fields'] == ['birth_date']
    assert prepare('registerClient', {**CLIENT, 'birth_date': '29.02.2000'})['complete']


@pytest.mark.parametrize('operation,values', [('updateClient', {'last_name': 'Тестов'}), ('updateClient', {'first_name': 'Аркадий'}),
                                             ('updateClient', {'birth_date': '19.06.1988'}), ('updateVisit', {'client_id': 7})])
def test_immutable_fields_are_forbidden_even_when_values_look_valid(operation, values):
    result = prepare(operation, values, path_parameters={'id': 7})
    assert result['invalid_fields'] == list(values)
    assert not result['complete']


def test_client_optional_null_clears_and_absence_stays_absent():
    assert prepare('updateClient', {'middle_name': None, 'comment': None}, path_parameters={'id': 7})['complete']
    assert prepare('updateClient', {}, path_parameters={'id': 7})['complete']
    assert prepare('updateClient', None, path_parameters={'id': 7})['missing_fields'] == ['body']
    assert prepare('updateVisit', {'status': None}, path_parameters={'id': 7})['invalid_fields'] == ['status']
    assert prepare('updateVisit', {'status': 'Подтверждена'}, path_parameters={'id': 7})['invalid_fields'] == ['status']


@pytest.mark.parametrize('query,invalid', [({'limit': 0}, ['limit']), ({'limit': 51}, ['limit']), ({'limit': True}, ['limit']),
                                          ({'limit': ''}, ['limit']), ({'offset': -1}, ['offset']),
                                          ({'offset': 9007199254740992}, ['offset']), ({'offset': 1.5}, ['offset'])])
def test_pagination_matches_api_limits_and_rejects_booleans(query, invalid):
    result = prepare('listVisits', query=query)
    assert result['invalid_fields'] == invalid
    assert not result['complete']


def test_query_and_path_http_scalars_are_accepted_without_body_coercion():
    result = prepare('listVisits', query={'client_id': 7, 'limit': '10', 'offset': '0', 'date_from': '', 'date_to': '24.10.2026'})
    assert result['complete']
    assert result['query'] == {'client_id': '7', 'limit': 10, 'offset': 0, 'date_from': '', 'date_to': '24.10.2026'}
    assert prepare('findClient', path_parameters={'id': 7})['path'] == '/clients/7'
    assert prepare('findClient')['missing_fields'] == ['id']
    for bad in [0, True, 7.0, '01', '../7', '7?x=1', '2147483648']:
        assert prepare('findClient', path_parameters={'id': bad})['invalid_fields'] == ['id']


@pytest.mark.parametrize('method,path', [('POST', '/appointments'), ('DELETE', '/visits/7'), ('POST', '/doctors'),
                                         ('GET', '/api/v1/doctors'), ('GET', 'https://example.org/doctors'),
                                         ('GET', '/doctors?limit=1'), ('GET', '/doctors/'), ('SELECT', '/clients')])
def test_only_real_method_route_pairs_can_be_constructed(method, path):
    with pytest.raises(ValueError):
        lookup_http(method, path)
    for identifier in ['SELECT * FROM patients', 'appointments.create', None]:
        with pytest.raises(ValueError):
            prepare(identifier)


def test_unknown_fields_invalid_containers_and_read_bodies_are_not_silently_discarded():
    assert prepare('createVisit', {**VISIT, 'sql': 'DROP TABLE patients'})['invalid_fields'] == ['sql']
    assert prepare('listDoctors', query={'sql': 'SELECT 1'})['invalid_fields'] == ['sql']
    assert prepare('registerClient', [])['invalid_fields'] == ['body']
    assert prepare('listDoctors', body={})['invalid_fields'] == ['body']
    assert prepare('listDoctors', query=[])['invalid_fields'] == ['query']
    assert prepare('findClient', path_parameters={7: 'id'})['invalid_fields'] == ['path_parameters']


def test_name_description_limits_and_comment_control_characters_are_enforced():
    assert prepare('registerClient', {**CLIENT, 'last_name': 'Т' * 101})['invalid_fields'] == ['last_name']
    assert prepare('registerClient', {**CLIENT, 'last_name': 'Тест\x00ов'})['invalid_fields'] == ['last_name']
    assert prepare('registerClient', {**CLIENT, 'last_name': '   '})['invalid_fields'] == ['last_name']
    result = prepare('updateClient', {'comment': 'Строка\nВторая\tстрока'}, path_parameters={'id': 7})
    assert result['complete']
    assert prepare('updateClient', {'comment': 'x' * 1001}, path_parameters={'id': 7})['invalid_fields'] == ['comment']
    assert prepare('createEscalation', {'client_id': 7, 'description': '  '})['invalid_fields'] == ['description']
    assert prepare('createEscalation', {'client_id': 7, 'description': 'x' * 1001})['invalid_fields'] == ['description']


def test_schema_validity_does_not_invent_availability_identity_or_authorization():
    # DBI explicitly accepts past moments and has no availability endpoint.
    result = prepare('createVisit', {**VISIT, 'scheduled_at': '01.01.2000 11:20'})
    assert result['complete'] is True
    assert result['authorized'] is False
    assert 'identity_verified' not in result
    # An inverted range is an allowed empty result, not an invalid request.
    assert prepare('listVisits', query={'date_from': '25.10.2026', 'date_to': '24.10.2026'})['complete']


def test_cache_does_not_share_mutable_output_and_parallel_reads_are_isolated(monkeypatch):
    mis_contract._load.cache_clear()
    original = Path.open
    reads = []
    def counted(path, *args, **kwargs):
        if path == mis_contract._SPEC_PATH:
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', counted)
    source = deepcopy(CLIENT)
    first = prepare('registerClient', source)
    first['body']['first_name'] = 'Изменено'
    first['missing_fields'].append('invented')
    assert source == CLIENT
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: prepare('registerClient', CLIENT), range(24)))
    assert all(r['body'] == CLIENT and not r['missing_fields'] for r in results)
    assert len(reads) == 1


def test_changed_specification_is_reloaded_without_mutating_the_shipping_file(tmp_path, monkeypatch):
    path = tmp_path / 'openapi.json'
    document = json.loads((ROOT / 'docs/openapi.json').read_text())
    path.write_text(json.dumps(document))
    monkeypatch.setattr(mis_contract, '_SPEC_PATH', path)
    assert prepare('listDoctors')['complete']
    document['paths'].pop('/doctors')
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='Unknown MIS operationId'):
        prepare('listDoctors')


def test_optional_blank_comments_and_normalized_name_lengths_follow_server_contract():
    # Client comments consisting only of whitespace map to null at the server;
    # they are explicitly accepted by its anyOf, including >1000 spaces.
    assert prepare('updateClient', {'comment': ' ' * 1001}, path_parameters={'id': 7})['complete']
    # NFC and whitespace normalisation happen before the name length limit.
    assert prepare('registerClient', {**CLIENT, 'last_name': 'Е\u0308' * 100})['complete']
    assert prepare('registerClient', {**CLIENT, 'last_name': '  Тестов  '})['complete']
    assert prepare('createEscalation', {'client_id': 7, 'description': '\n\t'})['invalid_fields'] == ['description']


def test_scenario_prepares_missing_or_ready_request_without_writing(tmp_path,monkeypatch):
    from datetime import date,datetime
    from calls.crm.registry import MockRegistry,seed_demo
    from clinic.crm import ClinicCrm
    monkeypatch.setenv('CRM_NOW','2026-10-15T11:00:00+04:00')
    monkeypatch.setenv('MODULE_SETTINGS_FILE',str(tmp_path/'modules.json'))
    registry=MockRegistry(str(tmp_path/'crm.sqlite'),datetime(2026,10,15,11))
    seed_demo(registry,date(2026,10,16));crm=ClinicCrm(registry)
    status,missing=crm.handle('prepare_mis',{'operation_id':'createVisit','client_id':7})
    assert status=='missing' and missing['missing_fields']==['doctor_id','scheduled_at']
    status,ready=crm.handle('prepare_mis',{'operation_id':'createVisit',**VISIT,'call_id':'C-01-test'})
    assert status=='ready' and ready['body']==VISIT and ready['authorized'] is False
    assert not registry.execute('SELECT * FROM bookings')
    status,invalid=crm.handle('prepare_mis',{'operation_id':'updateVisit','id':4,'client_id':999})
    assert status=='invalid' and invalid['invalid_fields']==['client_id']


@pytest.mark.parametrize('body,code',[
    ({'client_id':7},'MIS_MISSING_FIELDS'),
    ({**VISIT,'doctor_id':'3'},'MIS_INVALID_FIELDS'),
    ({**VISIT,'sql':'DELETE FROM visits'},'MIS_INVALID_FIELDS'),
])
def test_wire_constructor_rejects_invalid_fields_before_transport(monkeypatch,body,code):
    from app.types import MisError
    from clinic.crm import StrictStand
    stand=StrictStand('https://contract.invalid/api/v1','contract-only')
    def forbidden(*args,**kwargs):pytest.fail('Invalid request reached transport or consumed its rate budget')
    monkeypatch.setattr(stand.client,'_send',forbidden)
    monkeypatch.setattr(stand.bucket,'acquire',forbidden)
    with pytest.raises(MisError) as raised:stand.request('POST','/visits',body)
    assert raised.value.status==400 and raised.value.code==code


def test_valid_wire_request_uses_prepared_body_and_original_transport(monkeypatch):
    from clinic.crm import StrictStand
    stand=StrictStand('https://contract.invalid/api/v1','contract-only');sent=[]
    def transport(method,url,body,timeout,path):
        sent.append((method,url,json.loads(body),timeout,path))
        return 201,{},b'{"id": 123}'
    monkeypatch.setattr(stand.client,'_send',transport)
    assert stand.request('POST','/visits',VISIT)=={'id':123}
    assert sent==[('POST','https://contract.invalid/api/v1/visits',VISIT,5,'/visits')]

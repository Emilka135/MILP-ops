"""Deterministic, offline request construction from the checked-in MIS OpenAPI.

``complete`` means that the supplied fields fit the request contract only. It
never proves identity, consent, availability, referral or permission to mutate.
The module performs no network requests and never generates SQL or questions.
"""
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import quote


_SPEC_PATH = Path(__file__).resolve().parents[3] / 'docs' / 'openapi.json'
_METHODS = frozenset({'get', 'post', 'patch', 'put', 'delete'})
_NAME_FIELDS = frozenset({'last_name', 'first_name', 'middle_name'})
_DATE_FIELDS = frozenset({'birth_date', 'date_from', 'date_to'})


@lru_cache(maxsize=4)
def _load(path, modified, size):
    """Cache parsed operations; a changed specification gets a separate entry."""
    with Path(path).open(encoding='utf-8') as stream:
        spec = json.load(stream)
    operations = {}
    for template, methods in spec['paths'].items():
        for method, operation in methods.items():
            if method not in _METHODS:
                continue
            identifier = operation.get('operationId')
            if not identifier or identifier in operations:
                raise ValueError('MIS specification has missing or duplicate operationId')
            parameters = methods.get('parameters', []) + operation.get('parameters', [])
            operations[identifier] = (method.upper(), template, operation, parameters)
    return spec, operations


def _specification():
    info = _SPEC_PATH.stat()
    return _load(str(_SPEC_PATH), info.st_mtime_ns, info.st_size)


def _resolve(spec, schema):
    if '$ref' not in schema:
        return schema
    reference = schema['$ref']
    if not reference.startswith('#/'):
        raise ValueError('External MIS schema reference is forbidden')
    value = spec
    for segment in reference[2:].split('/'):
        value = value[segment.replace('~1', '/').replace('~0', '~')]
    return _resolve(spec, value)


def _valid(spec, schema, value):
    """The JSON Schema subset used by this OpenAPI; no implicit body coercion."""
    schema = _resolve(spec, schema)
    if value is None:
        return schema.get('nullable', False)
    kind = schema.get('type')
    if kind == 'integer' and type(value) is not int:
        return False
    if kind == 'number' and type(value) not in {int, float}:
        return False
    if kind == 'string' and not isinstance(value, str):
        return False
    if kind == 'boolean' and type(value) is not bool:
        return False
    if kind == 'object' and not isinstance(value, dict):
        return False
    if kind == 'array' and not isinstance(value, list):
        return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            return False
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            return False
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            return False
    if type(value) in {int, float}:
        if 'minimum' in schema and value < schema['minimum']:
            return False
        if 'maximum' in schema and value > schema['maximum']:
            return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get('required', [])):
            return False
        if any(key in value and not _valid(spec, rule, value[key])
               for key, rule in schema.get('properties', {}).items()):
            return False
    if 'allOf' in schema and not all(_valid(spec, rule, value) for rule in schema['allOf']):
        return False
    if 'anyOf' in schema and not any(_valid(spec, rule, value) for rule in schema['anyOf']):
        return False
    if 'not' in schema and _valid(spec, schema['not'], value):
        return False
    return True


def _field_valid(spec, name, schema, value):
    if not _valid(spec, schema, value):
        return False
    if value is None:
        return True
    # Name-length limits are in the API descriptions, rather than maxLength.
    # The server normalises whitespace/NFC before enforcing those limits.
    if name in _NAME_FIELDS and isinstance(value, str):
        normal = unicodedata.normalize('NFC', ' '.join(value.split()))
        if len(normal) > 100:
            return False
    if name in _DATE_FIELDS and isinstance(value, str) and value.strip():
        text, fmt, shape = value.strip(), '%d.%m.%Y', r'[0-9]{2}\.[0-9]{2}\.[0-9]{4}'
    elif name == 'scheduled_at' and isinstance(value, str):
        text, fmt, shape = value.strip(), '%d.%m.%Y %H:%M', r'[0-9]{2}\.[0-9]{2}\.[0-9]{4} [0-9]{2}:[0-9]{2}'
    else:
        return True
    if re.fullmatch(shape, text) is None:
        return False
    try:
        datetime.strptime(text, fmt)
    except ValueError:
        return False
    return True


def _immutable_fields(rule, values):
    """Identify fields rejected by the API's not(required)/not(anyOf) rules."""
    bad = set()
    required = rule.get('required', [])
    if required and all(key in values for key in required):
        bad.update(required)
    for branch in rule.get('anyOf', []):
        bad.update(_immutable_fields(branch, values))
    return bad


def _parameter_value(schema, value):
    """HTTP path/query values may arrive as strings; body values never coerce."""
    if isinstance(value, bool):
        return value
    if schema.get('type') == 'string' and type(value) is int:
        return str(value)
    if schema.get('type') == 'integer' and isinstance(value, str) and re.fullmatch(r'-?[0-9]+', value):
        return int(value)
    return value


def _mapping(value, name, invalid):
    if value is None:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        invalid.add(name)
        return {}
    return deepcopy(value)


def prepare(operation_id, body=None, query=None, path_parameters=None):
    """Build a known request, reporting structural missing/invalid field names.

    Unknown operation IDs raise ValueError. Unknown fields are invalid (stricter
    than the server's ignored extra body fields), so model output is not silently
    discarded. Optional middle_name/comment may be null. A missing required
    PATCH body is different from an explicitly supplied empty no-op object.
    """
    spec, operations = _specification()
    if not isinstance(operation_id, str) or operation_id not in operations:
        raise ValueError('Unknown MIS operationId')
    method, template, operation, parameters = operations[operation_id]
    missing, invalid = set(), set()
    query_values = _mapping(query, 'query', invalid)
    path_values = _mapping(path_parameters, 'path_parameters', invalid)
    checked_query, checked_path = {}, {}
    allowed_query, allowed_path = set(), set()
    for parameter in parameters:
        parameter = _resolve(spec, parameter)
        location, name = parameter['in'], parameter['name']
        if location not in {'query', 'path'}:
            continue
        values = query_values if location == 'query' else path_values
        checked = checked_query if location == 'query' else checked_path
        allowed = allowed_query if location == 'query' else allowed_path
        allowed.add(name)
        if name not in values:
            if parameter.get('required'):
                missing.add(name)
            continue
        schema = _resolve(spec, parameter['schema'])
        value = _parameter_value(schema, values[name])
        checked[name] = value
        if not _field_valid(spec, name, schema, value):
            invalid.add(name)
    invalid.update(set(query_values) - allowed_query)
    invalid.update(set(path_values) - allowed_path)

    request_body = _resolve(spec, operation.get('requestBody', {}))
    content = request_body.get('content', {}).get('application/json')
    checked_body = None
    if content:
        schema = _resolve(spec, content['schema'])
        values = _mapping(body, 'body', invalid)
        checked_body = deepcopy(body)
        missing.update(set(schema.get('required', [])) - set(values))
        if body is None and request_body.get('required') and not schema.get('required'):
            missing.add('body')
        properties = schema.get('properties', {})
        invalid.update(set(values) - set(properties))
        invalid.update(_immutable_fields(schema.get('not', {}), values))
        for name, value in values.items():
            if name in properties and not _field_valid(spec, name, properties[name], value):
                invalid.add(name)
    elif body is not None:
        invalid.add('body')
        checked_body = deepcopy(body)

    path = template
    for name, value in checked_path.items():
        if name not in invalid:
            path = path.replace('{' + name + '}', quote(str(value), safe=''))
    return {'operation_id': operation_id, 'method': method, 'path': path,
            'body': checked_body, 'query': checked_query,
            'missing_fields': sorted(missing), 'invalid_fields': sorted(invalid),
            'complete': not missing and not invalid, 'authorized': False}


def lookup_http(method, path, body=None, query=None):
    """Resolve an exact declared HTTP method/route and use the same constructor.

    Paths are relative to the configured server base (which includes /api/v1).
    Absolute URLs, embedded query strings, undeclared methods and invented
    endpoints are rejected. Parameter values still require schema validation.
    """
    if not isinstance(method, str) or not isinstance(path, str) or not path.startswith('/') or '?' in path or '#' in path:
        raise ValueError('Unknown MIS HTTP route')
    _, operations = _specification()
    for identifier, (verb, template, _, _) in operations.items():
        if method.upper() != verb:
            continue
        names = re.findall(r'\{([^{}]+)\}', template)
        pattern = re.escape(template)
        for name in names:
            pattern = pattern.replace(re.escape('{' + name + '}'), r'([^/]+)')
        match = re.fullmatch(pattern, path)
        if match:
            return prepare(identifier, body, query, dict(zip(names, match.groups())))
    raise ValueError('Unknown MIS HTTP route')

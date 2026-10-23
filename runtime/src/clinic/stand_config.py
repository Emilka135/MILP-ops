"""Resolve live credentials without ever attaching a real stand to demo patients."""

from urllib.parse import urlsplit


def settings(env):
    mode = env.get('APP_MODE', 'demo')
    url = env.get('STAND_URL', '').rstrip('/')
    token = env.get('STAND_TOKEN') or env.get('DBI_API_TOKEN', '')
    if mode == 'demo':
        # Explicit loopback stand is reserved for isolated contract tests.
        local = urlsplit(url)
        if env.get('ALLOW_LOCAL_STAND') == '1' and local.hostname in {'localhost', '127.0.0.1', '::1'}:
            return url, token
        return None
    if mode != 'live':
        raise ValueError('APP_MODE must be demo or live')
    url = url or 'https://crm-med-mock.dbi-testing.ru/api/v1'
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Live STAND_URL must be an HTTPS base URL')
    if not token:
        raise ValueError('DBI_API_TOKEN or STAND_TOKEN required in live mode')
    return url, token

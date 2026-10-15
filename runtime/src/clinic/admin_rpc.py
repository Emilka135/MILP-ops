"""Fixed module destinations; browser cannot select a URL or an arbitrary operation."""
import json
import os
import urllib.request
from calls.services.netutil import open_http


def module_call(module, operation, args):
    if module not in {'crm', 'tts'}:
        raise ValueError('Unsupported administration module')
    url = os.environ.get(module.upper()+'_URL', 'http://127.0.0.1:'+('8081' if module=='crm' else '8085'))
    request = urllib.request.Request(url.rstrip('/')+'/v1/'+operation,
        data=json.dumps(args,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST')
    try:
        with open_http(request, timeout=120 if operation=='bank.refresh' else 15) as response:
            result=json.loads(response.read(4*1024*1024))
    except OSError:
        raise ValueError('Модуль '+module+' недоступен; проверьте запуск контейнера') from None
    if result.get('status') not in {'ok','empty'}:
        raise ValueError(result.get('payload',{}).get('error') or result.get('status','Ошибка модуля'))
    return result.get('payload',{})

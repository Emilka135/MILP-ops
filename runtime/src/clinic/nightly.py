"""Night worker; the CRM enforces the same relational opening hours as callbacks."""
import json
import time
from .modules import CrmProxy


def run_once(crm):
    status,result=crm.call('learning.night',{})
    print(json.dumps({'job':'prepare_registrar_examples','status':status,**result},ensure_ascii=False),flush=True)
    return status,result


def run():
    crm=CrmProxy()
    while True:
        try:run_once(crm)
        except (OSError,ValueError) as exc:print(json.dumps({'job':'prepare_registrar_examples','status':'error','error':str(exc)},ensure_ascii=False),flush=True)
        time.sleep(60)

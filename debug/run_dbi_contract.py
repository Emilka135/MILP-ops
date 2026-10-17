"""One read-only team API request; never print credentials or patient records."""
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.env import load_env


def main():
    load_env(ROOT / '.env')
    token = os.environ.get('STAND_TOKEN') or os.environ.get('DBI_API_TOKEN')
    if not token:
        print('DBI_API_TOKEN or STAND_TOKEN is required'); return 1
    base = os.environ.get('STAND_URL') or 'https://crm-med-mock.dbi-testing.ru/api/v1'
    if not base.startswith('https://'):
        print('HTTPS required'); return 1
    request = urllib.request.Request(base.rstrip('/')+'/doctors?limit=1&offset=0', headers={'Authorization':'Bearer '+token})
    report = {'method':'GET','resource':'/doctors','mutations':0}
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310 -- validated HTTPS
            raw = response.read(64000); data = json.loads(raw)
            report.update(http_status=response.status, array=isinstance(data,list), count=len(data), fields=sorted(data[0]) if isinstance(data,list) and data else [])
    except urllib.error.HTTPError as error:
        report.update(http_status=error.code)
    except Exception as error:
        report.update(error=type(error).__name__)
    out = ROOT/'debug/runs/dbi-api'; out.mkdir(parents=True,exist_ok=True)
    report['bundled_contract_sha256'] = hashlib.sha256((ROOT/'docs/openapi.json').read_bytes()).hexdigest()
    (out/'read-only-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    return int(report.get('http_status') != 200 or not report.get('array'))


if __name__ == '__main__': raise SystemExit(main())

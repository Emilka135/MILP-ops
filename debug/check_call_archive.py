"""Read back a genuinely recorded consented call after container recreation."""
import http.cookiejar
import json
import os
from pathlib import Path
import urllib.request

folder=Path('debug/runs/admin-scenarios')
base=os.environ.get('EDITOR_URL','http://127.0.0.1:8090')
opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def api(route, body=None):
    request=urllib.request.Request(base+'/api/'+route, data=json.dumps(body).encode() if body is not None else None,
                                   headers={'Content-Type':'application/json'})
    with opener.open(request,timeout=15) as response:return json.load(response)
api('login',{'username':'admin','password':os.environ.get('EDITOR_PASSWORD','admin')})
voice=json.loads((folder/'voice-only/result.json').read_text())
doc=api('calls/detail?id='+voice['call_id'])
assert doc['consent_audio'] is True and len(doc['audio_files'])>=12
assert len([t for t in doc['transcript'] if t.get('source')=='real_asr'])==6
for name in doc['audio_files']:
    with opener.open(base+'/api/calls/audio?id='+voice['call_id']+'&file='+name,timeout=15) as response:
        raw=response.read()
    assert raw[:4]==b'RIFF' and len(raw)>100
report={'status':'passed','call_id':doc['call_id'],'archived_asr_turns':6,'audio_files_read_after_container_recreation':len(doc['audio_files']),
        'source':'Actual test-call archive in retained editor-data volume'}
(folder/'archive-after-restart.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))

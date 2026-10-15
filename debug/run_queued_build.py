"""Request a real build from the hosted editor and wait for its offline worker result."""
import http.cookiejar
import json
import os
from pathlib import Path
import time
import urllib.request


def main():
    base=os.environ.get('EDITOR_URL','http://127.0.0.1:8090')
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(route,body=None):
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(base+'/api/'+route,data=data,headers={'Content-Type':'application/json'})
        with opener.open(req,timeout=10) as response:return json.load(response)
    out=Path(__file__).resolve().parent/'runs/scenario-builder';out.mkdir(parents=True,exist_ok=True)
    for attempt in range(50):
        try:
            request('login',{'password':os.environ.get('EDITOR_PASSWORD','admin')})
            project=request('project')
            if project['build_enabled']:break
        except OSError:pass
        time.sleep(1)
    else:raise RuntimeError('Hosted editor build mode is not enabled')
    at=time.monotonic();job=request('build',{'revision':project['revision']})
    while job['status']=='running' and time.monotonic()-at<330:
        time.sleep(1);job=request('build')
    job['elapsed_seconds']=round(time.monotonic()-at,2)
    (out/'queued-container-result.json').write_text(json.dumps(job,ensure_ascii=False,indent=2)+'\n')
    assert job['status']=='succeeded',job
    assert len(job['engine_source_sha256'])==64
    print(json.dumps({k:v for k,v in job.items() if k!='log'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()

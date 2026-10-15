"""Resumable, bounded HTTP range download with upstream LFS SHA256 verification."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import time
import urllib.request
from check_space import require_space


def fetch(url, target, size, expected_sha, workers=8):
    target=Path(target)
    if target.exists() and target.stat().st_size==size and hashlib.sha256(target.read_bytes()).hexdigest()==expected_sha:
        return
    require_space(target.parent, size * 2)
    parts=target.parent/'.cache'/'ranges'/target.name
    parts.mkdir(parents=True,exist_ok=True)
    chunk_size=8*1024*1024

    def part(i):
        start=i*chunk_size;end=min(size,start+chunk_size)-1
        path=parts/str(i)
        if path.exists() and path.stat().st_size==end-start+1:
            return path
        for attempt in range(3):
            try:
                request=urllib.request.Request(url,headers={'Range':f'bytes={start}-{end}'})
                with urllib.request.urlopen(request,timeout=90) as response:
                    if response.headers.get('Content-Range')!=f'bytes {start}-{end}/{size}':
                        raise ValueError('server did not honour exact byte range')
                    data=response.read(end-start+2)
                if len(data)!=end-start+1:
                    raise ValueError('truncated model range')
                require_space(path.parent, len(data))
                path.write_bytes(data)
                print(f'{target.name}: chunk {i+1}/{(size+chunk_size-1)//chunk_size}',flush=True)
                return path
            except (OSError,ValueError):
                if attempt==2:raise
                time.sleep(1+attempt)
        raise RuntimeError('unreachable')

    with ThreadPoolExecutor(max_workers=workers) as pool:
        paths=list(pool.map(part,range((size+chunk_size-1)//chunk_size)))
    require_space(target.parent, size)
    temporary=target.with_suffix(target.suffix+'.download')
    digest=hashlib.sha256()
    with temporary.open('wb') as output:
        for path in paths:
            data=path.read_bytes();digest.update(data);output.write(data)
    if digest.hexdigest()!=expected_sha:
        temporary.unlink()
        raise ValueError('model SHA256 mismatch')
    temporary.replace(target)

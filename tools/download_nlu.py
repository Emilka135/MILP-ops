"""Fetch the pinned MIT E5 encoder, verifying every byte before activation."""
import hashlib
import json
from pathlib import Path
import urllib.request
from check_space import require_space

ROOT=Path(__file__).resolve().parents[1]
REVISION='761b726dd34fb83930e26aab4e9ac3899aa1fa78'
FILES={
 'tokenizer.json':(17082730,'0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'),
 'tokenizer_config.json':(443,'a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b'),
 'config.json':(658,'cb99455288675345e1a4f411438d5d0adbba5fbd3a67ea4fb03c015433b996c1'),
 'onnx/model_quantized.onnx':(118308185,'f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193'),
}
def main():
 root=ROOT/'modules/nlu/models/multilingual-e5-small'
 for name,(size,sha) in FILES.items():
  target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
  if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=sha:
   require_space(root,size*2)
   temp=target.with_suffix('.part')
   with urllib.request.urlopen('https://huggingface.co/Xenova/multilingual-e5-small/resolve/'+REVISION+'/'+name,timeout=90) as response,temp.open('wb') as stream:
    while chunk:=response.read(1024*1024):stream.write(chunk)
   if temp.stat().st_size!=size or hashlib.sha256(temp.read_bytes()).hexdigest()!=sha:raise ValueError('Encoder checksum mismatch: '+name)
   temp.replace(target)
  if target.stat().st_size!=size:raise ValueError('Encoder size mismatch: '+name)
 require_space()
 print(json.dumps({'model':'intfloat/multilingual-e5-small','conversion':'Xenova/multilingual-e5-small','revision':REVISION,'license':'MIT','files':FILES},indent=2))
if __name__=='__main__':main()

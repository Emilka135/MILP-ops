import { chromium as playwright } from 'playwright-core';
import chromium from '@sparticuz/chromium';
import { createRequire } from 'node:module';
import { readFile, mkdir, access } from 'node:fs/promises';
import { brotliDecompressSync } from 'node:zlib';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import os from 'node:os';
const require=createRequire(import.meta.url);
export async function launch(){
  const executablePath=process.env.CHROME_PATH || await chromium.executablePath();
  let env={...process.env};
  // Minimal Linux sandboxes may lack NSS. The pinned package ships these libs,
  // so builds don't need apt, root privileges, or a browser download from Google.
  if(!process.env.CHROME_PATH&&process.platform==='linux'){
    const libs=path.join(os.tmpdir(),'clinic-chromium-libs');
    try{await access(path.join(libs,'lib/libnss3.so'));}catch{
      await mkdir(libs,{recursive:true});
      const archive=path.resolve(path.dirname(require.resolve('@sparticuz/chromium')),'../bin/al2023.tar.br');
      execFileSync('tar',['-xf','-','-C',libs],{input:brotliDecompressSync(await readFile(archive)),maxBuffer:32*1024*1024});
    }
    env.LD_LIBRARY_PATH=[path.join(libs,'lib'),libs,env.LD_LIBRARY_PATH].filter(Boolean).join(':');
  }
  return playwright.launch({executablePath,args:chromium.args,headless:true,env});
}

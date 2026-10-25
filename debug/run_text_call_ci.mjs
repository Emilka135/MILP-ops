import {spawn} from 'node:child_process';
import {mkdir,readFile,writeFile,rm} from 'node:fs/promises';
const root=new URL('..',import.meta.url).pathname,out=root+'debug/runs/text-call-ci/';await mkdir(out,{recursive:true});await rm(out+'ready.json',{force:true});
const server=spawn(process.env.PYTHON||'python',[root+'debug/text_call_server.py','--out',out],{cwd:root,env:{...process.env,PYTHONPATH:root+'runtime/src:'+root},stdio:['ignore','pipe','pipe']});let logs='',test;
server.stdout.on('data',x=>logs+=x);server.stderr.on('data',x=>logs+=x);const deadline=setTimeout(()=>{server.kill('SIGTERM');test?.kill('SIGTERM')},150000);
try{let ready;for(let i=0;i<300;i++){try{ready=JSON.parse(await readFile(out+'ready.json','utf8'));break}catch{}if(server.exitCode!==null)throw Error(logs);await new Promise(r=>setTimeout(r,100));if(i===299)throw Error('Text backend startup timeout: '+logs)}
 test=spawn(process.execPath,[root+'debug/run_text_call_browser.mjs'],{cwd:root,env:{...process.env,TEXT_CALL_BASE:ready.base,TEXT_CALL_OUT:out,TEXT_CALL_COMPLETE_BOOKING:'1'},stdio:'inherit'});const code=await new Promise(resolve=>{test.once('exit',resolve);test.once('error',()=>resolve(1))});if(code!==0)throw Error('Actual text browser failed: '+code)
}finally{clearTimeout(deadline);server.kill('SIGTERM');await new Promise(resolve=>{if(server.exitCode!==null)return resolve();const timer=setTimeout(()=>{server.kill('SIGKILL');resolve()},5000);server.once('exit',()=>{clearTimeout(timer);resolve()})});await writeFile(out+'server.log',logs)}

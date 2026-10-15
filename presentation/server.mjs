import http from 'node:http';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const execute=promisify(execFile);
let building=false;
import { readFile, writeFile, rename } from 'node:fs/promises';
import { html, loadDeck, validate, root, outputs } from './lib.mjs';
import { gallery } from './gallery.mjs';
const port=Number(process.env.PORT||8787);
function json(res,status,data){res.writeHead(status,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(data));}
const downloads=new Map([['clinic-voice-assistant.pdf','application/pdf'],['clinic-voice-assistant.pptx','application/vnd.openxmlformats-officedocument.presentationml.presentation'],['clinic-voice-assistant-editable.pptx','application/vnd.openxmlformats-officedocument.presentationml.presentation']]);
http.createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,'http://preview');
    const assets = {
      '/assets/styles.css': ['src/styles.css','text/css; charset=utf-8'],
      '/assets/viewer.js': ['src/viewer.js','application/javascript; charset=utf-8'],
      '/assets/NotoSans-Regular.ttf': ['assets/NotoSans-Regular.ttf','font/ttf'],
      '/assets/NotoSans-Bold.ttf': ['assets/NotoSans-Bold.ttf','font/ttf']
    };
    if(req.method==='GET'&&url.pathname==='/assets/fonts.css'){
      res.writeHead(200,{'Content-Type':'text/css; charset=utf-8','Cache-Control':'no-store'});
      res.end('@font-face{font-family:Deck;src:url(/assets/NotoSans-Regular.ttf);font-weight:400;font-display:swap}@font-face{font-family:Deck;src:url(/assets/NotoSans-Bold.ttf);font-weight:700;font-display:swap}');return;
    }
    if(req.method==='GET'&&assets[url.pathname]){
      const [file,type]=assets[url.pathname];const data=await readFile(path.join(root,file));
      res.writeHead(200,{'Content-Type':type,'Cache-Control':'no-store'});res.end(data);return;
    }
    if(req.method==='GET'&&(url.pathname==='/'||url.pathname==='/index.html')){
      res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'});
      res.end(gallery(await loadDeck(),url.searchParams.get('slide')||1));return;
    }
    if(req.method==='GET'&&url.pathname==='/editor'){
      res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'});
      res.end(await html(await loadDeck(),{editable:true}));return;
    }
    if(req.method==='GET'&&/^\/slides\/slide-\d{2,3}\.png$/.test(url.pathname)){
      const file=path.join(outputs,'slides',path.basename(url.pathname));
      try{const data=await readFile(file);res.writeHead(200,{'Content-Type':'image/png','Cache-Control':'no-store'});res.end(data);}
      catch{json(res,404,{error:'Слайд не собран. Откройте редактор и нажмите «Экспорт».'});}return;
    }
    if(req.method==='GET'&&url.pathname==='/api/deck'){json(res,200,await loadDeck());return;}
    if(req.method==='PUT'&&url.pathname==='/api/deck'){
      const origin=req.headers.origin;
      if(origin&&new URL(origin).host!==req.headers.host){json(res,403,{error:'Запись только из этого редактора'});return;}
      if(!req.headers['content-type']?.startsWith('application/json')){json(res,415,{error:'Ожидается JSON'});return;}
      let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>5_000_000){json(res,413,{error:'Слишком большой файл'});return;}}
      let deck;try{deck=validate(JSON.parse(body));}catch(e){json(res,400,{error:e.message});return;}
      const tmp=path.join(root,`deck-${randomUUID()}.tmp`);await writeFile(tmp,JSON.stringify(deck,null,2)+'\n');await rename(tmp,path.join(root,'deck.json'));json(res,200,{saved:true});return;
    }
    if(req.method==='POST'&&url.pathname==='/api/build'){
      const origin=req.headers.origin;
      if(origin&&new URL(origin).host!==req.headers.host){json(res,403,{error:'Сборка только из этого редактора'});return;}
      if(building){json(res,409,{error:'Сборка уже идёт'});return;}
      building=true;
      try{const {stdout}=await execute(process.execPath,['build.mjs'],{cwd:root,timeout:120000,maxBuffer:2_000_000});json(res,200,{built:true,log:stdout});}
      catch(e){console.error(e.stderr||e.message);json(res,500,{error:'Ошибка экспорта. Подробности в логе сервера.'});}
      finally{building=false;}
      return;
    }
    if(req.method==='GET'&&url.pathname.startsWith('/downloads/')){const file=url.pathname.slice('/downloads/'.length);if(downloads.has(file)){const data=await readFile(path.join(outputs,file));res.writeHead(200,{'Content-Type':downloads.get(file)});res.end(data);return;}}
    json(res,404,{error:'Не найдено'});
  }catch(e){console.error(e.message);json(res,500,{error:'Не удалось обработать запрос'});}
}).listen(port,'0.0.0.0',()=>console.log(`Presentation editor: http://0.0.0.0:${port}`));

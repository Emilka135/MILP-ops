import test from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { launch } from '../browser.mjs';
import { outputs, root, loadDeck } from '../lib.mjs';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

const expectedSlides = (await loadDeck()).slides.length;

test('HTML: все слайды без переполнений, встроенные шрифты, навигация и редактор',async()=>{
 const browser=await launch();
 try{
  const page=await browser.newPage({viewport:{width:1440,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(pathToFileURL(path.join(outputs,'index.html')).href+'?export=1');await page.evaluate(()=>window.deckReady);
  assert.equal(await page.locator('.slide').count(),expectedSlides);
  assert.deepEqual(await page.locator('.text').evaluateAll(es=>es.filter(e=>e.scrollHeight>e.clientHeight+1||e.scrollWidth>e.clientWidth+1).map(e=>e.dataset.id)),[]);
  assert.equal(await page.evaluate(()=>document.fonts.check('700 16px Deck')),true);
  await page.goto(pathToFileURL(path.join(outputs,'index.html')).href);await page.evaluate(()=>window.deckReady);
  await page.keyboard.press('ArrowRight');assert.equal(await page.locator('#counter').innerText(),`2 / ${expectedSlides}`);
  await page.click('#edit');assert.equal(await page.locator('#inspector').isVisible(),true);
  const heading=page.locator('.slide.active .text').filter({hasText:'Всем нужен талон утром.'}).first();await heading.click();
  await page.locator('textarea[name=text]').fill('Тест редактирования');assert.ok((await page.locator('.slide.active').innerText()).includes('Тест редактирования'));
  await page.setViewportSize({width:390,height:844});assert.ok(await page.locator('#viewport').isVisible());
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});

test('сервер принимает preview-host, отдаёт JSON и отклоняет повреждённые записи',async()=>{
 const server=spawn(process.execPath,['server.mjs'],{cwd:root,env:{...process.env,PORT:'18787'},stdio:['ignore','pipe','pipe']});
 try{
  await Promise.race([once(server.stdout,'data'),once(server,'exit').then(()=>{throw Error('Сервер не запустился');}),new Promise((_,rej)=>{const timer=setTimeout(()=>rej(Error('Таймаут старта')),10000);timer.unref();})]);
  const base='http://127.0.0.1:18787';
  assert.equal((await fetch(base,{headers:{Host:'8787-preview.e2b.app'}})).status,200);
  assert.equal((await(await fetch(base+'/api/deck')).json()).slides.length,expectedSlides);
  assert.equal((await fetch(base+'/api/deck',{method:'PUT',headers:{'Content-Type':'application/json'},body:'{}'})).status,400);
  assert.equal((await fetch(base+'/api/deck',{method:'PUT',headers:{'Content-Type':'application/json',Origin:'https://other.example'},body:'{}'})).status,403);
  assert.equal((await fetch(base+'/downloads/clinic-voice-assistant.pdf')).status,200);
  assert.equal((await fetch(base+'/downloads/../../deck.json')).status,404);
 }finally{server.kill();await once(server,'exit');}
});

test('preview: прямой вход с hash, запрет history, скрытый iframe и отключённый JS не дают пустой экран',async()=>{
 const server=spawn(process.execPath,['server.mjs'],{cwd:root,env:{...process.env,PORT:'18788'},stdio:['ignore','pipe','pipe']});
 let browser;
 try{
  await once(server.stdout,'data');browser=await launch();
  const page=await browser.newPage({viewport:{width:1440,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{history.replaceState=()=>{throw new DOMException('Sandbox restriction','SecurityError');};});
  for(const hash of ['#/preview','#slide-1','#13','#999','#0']){
   await page.goto('about:blank');await page.goto('http://127.0.0.1:18788/editor'+hash);assert.equal(await page.evaluate(()=>window.deckReady),true);
   assert.equal(await page.locator('.slide.active').count(),1);
   assert.ok(await page.locator('#viewport').evaluate(e=>e.clientHeight>100));
  }
  await page.goto('about:blank');
  await page.setContent('<iframe id="preview" sandbox="allow-scripts allow-downloads" src="http://127.0.0.1:18788/editor#/preview" style="display:none;width:1100px;height:700px"></iframe>');
  const handle=await page.locator('#preview').elementHandle();const frame=await handle.contentFrame();
  await frame.waitForFunction(()=>!!window.deckReady);await frame.evaluate(()=>window.deckReady);
  await page.locator('#preview').evaluate(e=>e.style.display='block');
  await frame.waitForFunction(()=>document.querySelector('#viewport').clientHeight>100 && getComputedStyle(document.querySelector('#slides')).transform!=='none');
  const scale=await frame.locator('#slides').evaluate(e=>getComputedStyle(e).transform);
  assert.notEqual(scale,'matrix(0, 0, 0, 0, 0, 0)');assert.notEqual(scale,'none');
  assert.equal(await frame.locator('.slide.active').count(),1);
  const nojs=await browser.newContext({javaScriptEnabled:false});const fallback=await nojs.newPage();
  await fallback.goto('http://127.0.0.1:18788/editor');
  assert.equal(await fallback.locator('.slide.active').count(),1);
  assert.ok((await fallback.locator('.slide.active').innerText()).includes('Очередь на месяц.'));
  assert.ok(await fallback.locator('#viewport').evaluate(e=>e.clientHeight>100));
  await fallback.goto(`http://127.0.0.1:18788/?slide=${expectedSlides-1}`);
  assert.ok(await fallback.locator('.slide-image').evaluate(e=>e.complete&&e.naturalWidth>0));
  await fallback.getByRole('link',{name:'Далее →',exact:true}).click();
  assert.equal(new URL(fallback.url()).searchParams.get('slide'),String(expectedSlides));
  assert.ok(await fallback.locator('.slide-image').evaluate(e=>e.complete&&e.naturalWidth>0));
  assert.equal(await fallback.locator('script').count(),0);
  assert.deepEqual(errors,[]);
 }finally{if(browser)await browser.close();server.kill();await once(server,'exit');}
});

/** Real browser -> deployed editor -> HTTP HTML fixture and supplied source exports. */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
const out=new URL('./runs/data-sources/browser/',import.meta.url).pathname;await mkdir(out,{recursive:true});
const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
const page=await browser.newPage({viewport:{width:1640,height:1080}});const errors=[],checks=[];page.on('pageerror',e=>errors.push(String(e)));
let added,fixtureName;
try{
 const site=out+'site';await mkdir(site,{recursive:true});
 await writeFile(site+'/info.html','<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Проверка сайта-источника</title></head><body><h1>Демонстрационная страница источника</h1><p>Эта страница проверяет чтение сайта из административной панели.</p><script>window.thisMustNeverExecute=true;</script></body></html>');
 const name='clinic-voice-source-fixture-'+Date.now();
 const launched=spawnSync('docker',['run','--rm','-d','--name',name,'--network','clinic-voice_clinic','--mount','type=bind,src='+site+',dst=/site,readonly','--entrypoint','python3','clinic-voice-scenario-editor','-m','http.server','8099','--bind','0.0.0.0','--directory','/site'],{encoding:'utf8'});
 assert.equal(launched.status,0,launched.stderr);fixtureName=name;
 const sourceUrl='http://'+name+':8099/info.html';
 await page.goto('http://127.0.0.1:8090');
 const login=await page.evaluate(async()=>{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'admin'})});return r.status});assert.equal(login,200);
 await page.reload();await page.getByRole('button',{name:'Настройки',exact:true}).click();
 const panel=page.getByRole('region',{name:'Источники данных'});await panel.getByRole('heading',{name:'Источники данных',exact:true}).waitFor();
 await panel.locator('fieldset').first().waitFor();
 assert.ok((await panel.getByLabel(/Путь или ссылка/).evaluateAll(inputs=>inputs.map(input=>input.value))).some(p=>p.includes('Расписание_врачей')));
 await panel.getByRole('button',{name:'Добавить источник',exact:true}).click();
 const card=panel.locator('fieldset').last();added=(await card.locator('input[aria-label^="Путь или ссылка"]').getAttribute('aria-label')).replace('Путь или ссылка ','');
 await card.locator('input[aria-label^="Название источника"]').fill('Проверка сайта через браузер');
 await card.locator('input[aria-label^="Путь или ссылка"]').fill(sourceUrl);
 await card.getByRole('button',{name:'Проверить и показать',exact:true}).click();
 await card.locator('.source-preview').waitFor();const preview=await card.locator('.source-preview').innerText();assert.ok(preview.includes('Демонстрационная страница источника'));assert.ok(!preview.includes('thisMustNeverExecute'));
 await page.screenshot({path:out+'01-site-source-preview.png',fullPage:true});checks.push('Administrator enters and reads an actual HTML URL; scripts are not executed or shown');
 await page.reload();await page.getByRole('button',{name:'Настройки',exact:true}).click();await panel.locator('legend').filter({hasText:'Проверка сайта через браузер'}).waitFor();checks.push('Source URL and preview survive reload');
 const restored=panel.locator('fieldset').filter({hasText:'Проверка сайта через браузер'});
 await restored.getByLabel('Использовать источник',{exact:true}).uncheck();await panel.getByRole('button',{name:'Сохранить источники',exact:true}).click();await panel.getByText(/Ссылки сохранены/).waitFor();checks.push('The website source can be fully disabled');
 await panel.getByRole('button',{name:'Обновить таблицы из источников',exact:true}).click();await panel.getByText(/Таблицы обновлены: 85 врачей, 136973 слотов/).waitFor({timeout:120000});checks.push('All three supplied file sources rebuild and activate the real SQLite snapshot');
 await page.screenshot({path:out+'02-active-file-sources.png',fullPage:true});
 const active=await page.evaluate(async()=>await(await fetch('/api/settings/sources')).json());assert.ok(active.active.sources.every(s=>s.path.startsWith('/source-files/')));assert.equal(active.active.sources.length,3);assert.equal(active.active.reference_sources.length,0);
 await page.goto('http://127.0.0.1:8090/registrar');await page.getByRole('button',{name:'Начать пробный звонок',exact:true}).click();
 const probe=await page.evaluate(async()=>{const list=await(await fetch('/api/workshop/calls?registrar=100')).json();return list.calls.find(c=>c.archive_source==='local')});assert.ok(probe);checks.push('Test-call UI remains available after snapshot activation');
 assert.deepEqual(errors,[]);
 await writeFile(out+'result.json',JSON.stringify({status:'passed',checks,errors,active_sources:active.active.sources.map(s=>({kind:s.kind,path:s.path,row_count:s.row_count}))},null,2));console.log(JSON.stringify({status:'passed',checks},null,2));
}catch(error){await page.screenshot({path:out+'failure.png',fullPage:true});await writeFile(out+'result.json',JSON.stringify({status:'failed',error:String(error),checks,errors},null,2));throw error}
finally{
 // Remove only our own temporary source, preserving any concurrent administrator changes.
 if(added)await page.evaluate(async id=>{const get=await fetch('/api/settings/sources');if(!get.ok)return;const current=await get.json();await fetch('/api/settings/sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sources:current.sources.filter(s=>s.id!==id),revision:current.revision})})},added).catch(()=>{});
 await browser.close();
 if(fixtureName)spawnSync('docker',['rm','-f',fixtureName],{encoding:'utf8'});
}

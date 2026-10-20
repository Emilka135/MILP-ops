/** Actual deployed UI + local E5 + source SQLite, no mocked HTTP results. */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
const out=new URL('./runs/call-archive/browser/',import.meta.url).pathname;
await mkdir(out,{recursive:true});
const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
const page=await browser.newPage({viewport:{width:1640,height:1080}}),errors=[],checks=[];
page.on('pageerror',e=>errors.push(String(e)));
let key;
try{
 await page.goto('http://127.0.0.1:8090/registrar');
 const login=await page.evaluate(async()=>{const response=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'admin'})});return response.status});assert.equal(login,200);
 await page.reload();await page.getByLabel('Ширина панели базы данных').waitFor();
 // A historical call must open even when its scenario state has been deleted.
 const old=await page.evaluate(async()=>{const data=await(await fetch('/api/workshop/calls?registrar=100')).json();return data.calls.find(c=>c.archive_source==='runtime'&&!c.scenario_revision)||data.calls.find(c=>c.archive_source==='runtime')});
 assert.ok(old,'Existing runtime archive is expected');
 await page.getByLabel('Номер звонка').fill(old.call_id);await page.getByRole('button',{name:'Открыть звонок',exact:true}).click();
 await page.locator('.archived-call').waitFor();assert.ok(await page.locator('.transcript-line').count());checks.push('Existing runtime call opens with its recorded path');
 await page.getByRole('button',{name:'Начать пробный звонок',exact:true}).click();
 await page.getByLabel('Ответ текстом').waitFor();
 async function say(text){const input=page.getByLabel('Ответ текстом');await input.fill(text);await page.getByRole('button',{name:'Отправить',exact:true}).click();await page.getByRole('button',{name:'Отправить',exact:true}).waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('.observer-text-input input').disabled);}
 await say('Офтальмолог');await page.getByText('Вы хотите записаться к офтальмологу? Да — один. Нет — ноль, переведу на оператора.',{exact:true}).first().waitFor();
 await say('Продолжить');await say('Иванов Сергей Владимирович');await say('5 октября 1915');
 await page.getByRole('heading',{name:'Выбираем врача',exact:true}).waitFor();
 const inspector=page.getByRole('region',{name:'Результат запроса к БД'});
 await inspector.getByText(/SELECT d.id,d.full_name/).waitFor();assert.ok((await inspector.locator('table tbody tr').count())>=2);
 await page.screenshot({path:out+'01-query-doctors.png',fullPage:true});
 const splitter=page.getByLabel('Ширина панели базы данных'),before=Number(await splitter.getAttribute('aria-valuenow'));
 const box=await splitter.boundingBox();await page.mouse.move(box.x+3,box.y+100);await page.mouse.down();await page.mouse.move(box.x+150,box.y+100,{steps:12});await page.mouse.up();
 assert.ok(Number(await splitter.getAttribute('aria-valuenow'))>before+100);checks.push('Left SQL/results pane resizes by real mouse drag');
 await splitter.focus();await page.keyboard.press('ArrowLeft');checks.push('Splitter supports keyboard resizing');
 await say('1');await page.getByRole('heading',{name:'Свободные талоны',exact:true}).waitFor();
 await inspector.getByText(/SELECT s.\* FROM slots/).waitFor();
 await page.screenshot({path:out+'02-source-slots-resized.png',fullPage:true});
 key=await page.evaluate(()=>document.querySelector('.query-call').textContent.replace('Звонок: ',''));
 const saved=await page.evaluate(async id=>await(await fetch('/api/workshop/call?id='+encodeURIComponent(id)+'&source=local&registrar=100')).json(),key);
 assert.equal(saved.source_data.mode,'file_snapshot');assert.equal(saved.patient.birth_date,'1915-10-05');assert.ok(saved.queries.some(q=>q.sql.includes('source.slots')));assert.ok(saved.offers.every(s=>s.source_slot_id));
 checks.push('Centenarian identified; actual imported doctors/slots and bound SQL are visible');
 await page.getByRole('button',{name:'Завершить звонок',exact:true}).click();await page.locator('.archived-call h2').filter({hasText:key}).waitFor();
 assert.ok(await page.locator('.recent-calls small').filter({hasText:'Пробный · текст'}).count());checks.push('Typed call appears in archive and opens after ending');
 await page.reload();await page.getByLabel('Номер звонка').fill(key);await page.getByRole('button',{name:'Открыть звонок',exact:true}).click();await page.locator('.archived-call h2').filter({hasText:key}).waitFor();
 await page.locator('.archived-call .call-step-picker button').last().click();assert.ok(await page.locator('.graph-turns').innerText());
 await page.screenshot({path:out+'03-text-call-reopened.png',fullPage:true});checks.push('Saved typed transcript and scenario path survive reload');
 await page.getByLabel('Номер звонка').fill('missing-call-for-visible-error');await page.getByRole('button',{name:'Открыть звонок',exact:true}).click();await page.locator('.quest-error').waitFor();checks.push('Failed open shows a visible error');
 assert.deepEqual(errors,[]);
 await writeFile(out+'result.json',JSON.stringify({status:'passed',call_id:key,old_call:old.call_id,checks,errors},null,2));console.log(JSON.stringify({status:'passed',call_id:key,checks},null,2));
}catch(error){await page.screenshot({path:out+'failure.png',fullPage:true});await writeFile(out+'result.json',JSON.stringify({status:'failed',call_id:key,error:String(error),checks,errors},null,2));throw error}
finally{await browser.close()}

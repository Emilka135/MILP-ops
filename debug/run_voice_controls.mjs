/** Real browser playback, anonymous read-only call detail, user/user and admin. */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const editor=process.env.EDITOR_URL||'http://127.0.0.1:28490',voice=process.env.ORCHESTRATOR_URL||'http://127.0.0.1:28480';
const out=new URL('./runs/voice-controls/',import.meta.url);await mkdir(out,{recursive:true});
const call_id='voice-controls-'+Date.now();
async function command(op,args){const r=await fetch(voice+'/v1/'+op,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({call_id,...args})});assert.ok(r.ok);return (await r.json()).payload}
await command('call.start',{flow:'inbound',registrar_number:'100',caller_phone:'79990000003'});await command('call.dtmf',{digit:'*'});
const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});let report;const errors=[];
try{
 const page=await browser.newPage({viewport:{width:1600,height:1080}});page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(editor+'/registrar');await page.getByText('Наблюдатель · Рабочее место 100',{exact:true}).waitFor();
 await page.getByRole('button',{name:'Озвучить сообщение'}).click();await page.waitForFunction(()=>{const a=document.querySelector('.speech-preview audio');return a&&!a.paused&&a.currentTime>0});
 await page.getByLabel('Номер звонка').fill(call_id);await page.getByRole('button',{name:'Открыть звонок',exact:true}).click();await page.locator('.call-transcript .transcript-line').first().waitFor();
 assert.equal(await page.getByRole('button',{name:'Улучшить этот шаг'}).count(),0);
 const denied=await page.evaluate(async()=> (await fetch('/api/workshop/propose',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status);assert.equal(denied,403);
 await page.screenshot({path:new URL('01-observer-detail-and-audio.png',out).pathname,fullPage:true});
 await page.getByRole('button',{name:'Войти',exact:true}).click();await page.getByText('Ваш логин',{exact:true}).locator('input').fill('user');await page.getByText('Пароль',{exact:true}).locator('input').fill('user');await page.getByRole('button',{name:'Войти в мастерскую'}).click();await page.getByText('Рабочее место 100',{exact:true}).waitFor();
 // Actual source-rate preview, not browser speechSynthesis and not a silent stub.
 let sourceRate;page.on('response',async r=>{if(r.url().endsWith('/api/workshop/preview')&&r.ok())sourceRate=(await r.json()).sample_rate});
 await page.getByRole('button',{name:'Озвучить сообщение'}).click();await page.waitForFunction(()=>{const a=document.querySelector('.speech-preview audio');return a&&!a.paused&&a.currentTime>0});assert.equal(sourceRate,48000);
 await page.screenshot({path:new URL('02-user-source-quality-playback.png',out).pathname,fullPage:true});
 const context=await browser.newContext({viewport:{width:1600,height:1080}});const admin=await context.newPage();admin.on('pageerror',e=>errors.push(String(e)));await admin.goto(editor);await admin.getByLabel('Пароль администратора').fill('admin');await admin.getByRole('button',{name:'Подключиться',exact:true}).click();await admin.locator('.react-flow__node').first().waitFor();
 const project=await admin.evaluate(async()=> (await fetch('/api/project')).json());const flow=project.flows.find(f=>f.name==='inbound');
 await admin.getByLabel('Поиск по всем сценариям').fill('inbound.welcome');await admin.locator('.search-result').first().click();
 await admin.getByLabel('Текст сообщения').waitFor();await admin.locator('.inspector .speech-preview button').click();await admin.waitForFunction(()=>{const a=document.querySelector('.inspector .speech-preview audio');return a&&!a.paused&&a.currentTime>0});
 await admin.screenshot({path:new URL('03-admin-message-playback.png',out).pathname,fullPage:true});
 await admin.getByRole('button',{name:'Сотрудники',exact:true}).click();await admin.getByLabel('Имя сотрудника',{exact:true}).fill('Проверка формы');await admin.getByLabel('Логин',{exact:true}).fill('voice_ui_102');await admin.getByLabel('Рабочий номер',{exact:true}).fill('102');await admin.getByLabel('Пароль, не меньше 12 символов',{exact:true}).fill('browser temporary password');await admin.getByRole('button',{name:'Выдать доступ',exact:true}).click();await admin.getByRole('status').filter({hasText:'Доступ создан'}).waitFor();await admin.screenshot({path:new URL('04-admin-account-created.png',out).pathname,fullPage:true});
 assert.deepEqual(errors,[]);report={status:'passed',call_id,observer_real_audio:true,observer_call_detail:true,observer_write_denied:true,user_user_login:true,user_preview_sample_rate:sourceRate,admin_message_played:true,account_form_created:true,errors};console.log(report);
}catch(e){report={status:'failed',error:String(e),errors};const pages=browser.contexts().flatMap(c=>c.pages());await pages.at(-1)?.screenshot({path:new URL('failure.png',out).pathname,fullPage:true});throw e}
finally{await writeFile(new URL('result.json',out),JSON.stringify(report,null,2)+'\n');await browser.close()}

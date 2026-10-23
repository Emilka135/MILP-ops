/** Actual Silero -> actual ASR -> Rust call archive -> registrar's adjacent skill workshop. */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const root=path.resolve(new URL('..',import.meta.url).pathname),out=path.join(root,'debug/runs/workshop/browser');
await mkdir(out,{recursive:true});
const voice=process.env.ORCHESTRATOR_URL||'http://127.0.0.1:28480',editor=process.env.EDITOR_URL||'http://127.0.0.1:28490';
const call_id='workshop-'+Date.now();
async function command(op,args){const res=await fetch(voice+'/v1/'+op,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({call_id,...args})});const data=await res.json();assert.ok(res.ok,JSON.stringify(data));return data.payload}
let browser;const errors=[];
try{
 await command('call.start',{flow:'inbound',registrar_number:'101',caller_phone:'79990000003'});
 await command('call.timeout',{});
 const speech=await command('speech.synth',{text:'Перенесите запись.',rate:1.5});
 const pcm=Buffer.from(speech.pcm_b64,'base64');assert.ok(pcm.length>1600);assert.ok(pcm.some(x=>x!==0));
 // Save the actual input as a playable WAV; no text injection into ASR.
 const header=Buffer.alloc(44);header.write('RIFF');header.writeUInt32LE(36+pcm.length,4);header.write('WAVEfmt ',8);header.writeUInt32LE(16,16);header.writeUInt16LE(1,20);header.writeUInt16LE(1,22);header.writeUInt32LE(8000,24);header.writeUInt32LE(16000,28);header.writeUInt16LE(2,32);header.writeUInt16LE(16,34);header.write('data',36);header.writeUInt32LE(pcm.length,40);await writeFile(path.join(out,'input-reschedule.wav'),Buffer.concat([header,pcm]));
 await command('call.audio',{pcm_b64:speech.pcm_b64,rate:8000});await command('call.dtmf',{digit:'*'});
 browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 const admin=await browser.newPage();await admin.goto(editor);
 await admin.getByLabel('Пароль администратора').fill('admin');await admin.getByRole('button',{name:'Подключиться',exact:true}).click();await admin.locator('.react-flow__node').first().waitFor();
 const account=await admin.evaluate(async()=>{const r=await fetch('/api/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'workshop101',registrar:'101',password:'workshop test password',label:'Тестовое рабочее место'})});return {status:r.status,data:await r.json()}});assert.equal(account.status,200);
 const context=await browser.newContext({viewport:{width:1650,height:1080}});const page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(editor+'/registrar');await page.getByRole('button',{name:'Войти',exact:true}).click();await page.getByText('Ваш логин',{exact:true}).locator('input').fill('workshop101');await page.getByText('Пароль',{exact:true}).locator('input').fill('workshop test password');await page.getByRole('button',{name:'Войти в мастерскую'}).click();
 await page.getByText('Рабочее место 101',{exact:true}).waitFor();await page.getByLabel('Номер звонка').fill(call_id);await page.getByRole('button',{name:'Открыть звонок',exact:true}).click();await page.locator('.transcript-line.patient').first().waitFor();
 const actual=await page.locator('.transcript-line.patient p').first().innerText();assert.ok(actual.toLowerCase().includes('перенес'),actual);
 await page.locator('.transcript-line.patient').first().click();await page.waitForTimeout(250);
 await page.screenshot({path:path.join(out,'01-neighbor-skills.png'),fullPage:true});assert.equal(await page.locator('.react-flow').count(),0);
 await page.getByRole('button',{name:'Улучшить этот шаг'}).click();await page.getByLabel('Название случая').fill('Перенос приёма на утро');await page.getByLabel('Другие формулировки').fill(actual+'\nМне бы передвинуть приём');await page.getByLabel('Правильный ответ').fill('Давайте подберём для вас другое время приёма.');await page.getByLabel('Когда ответить').selectOption('inbound.welcome');
 await page.getByRole('button',{name:'Пройти новый вариант'}).click();await page.screenshot({path:path.join(out,'02-propose-earlier-response.png'),fullPage:true});
 await page.getByRole('button',{name:'Предложить исправление',exact:true}).click();await page.getByText('Новый пример собран!',{exact:true}).waitFor();await page.screenshot({path:path.join(out,'03-earned-example.png'),fullPage:true});
 const checks=await page.evaluate(async()=>{const project=await fetch('/api/project');const cases=await(await fetch('/api/workshop/cases?registrar=102')).json();const calls=await(await fetch('/api/workshop/calls?registrar=102')).json();return {project_status:project.status,cases,calls}});
 assert.equal(checks.project_status,403);assert.equal(checks.cases.cases[0].registrar,'101');assert.equal(checks.cases.cases[0].status,'queued');assert.equal(checks.cases.badges.in_use,false);assert.ok(checks.calls.calls.some(c=>c.call_id===call_id));assert.deepEqual(errors,[]);
 await writeFile(path.join(out,'result.json'),JSON.stringify({status:'passed',call_id,actual_asr:actual,real_audio:true,staff_cannot_edit_system:true,number_cannot_be_overridden:true,proposal_status:'queued',activation_claimed:false,errors},null,2)+'\n');console.log('Workshop acceptance passed:',call_id,actual);
}catch(error){if(browser){const pages=browser.contexts().flatMap(c=>c.pages());await pages.at(-1)?.screenshot({path:path.join(out,'failure.png'),fullPage:true})}await writeFile(path.join(out,'result.json'),JSON.stringify({status:'failed',call_id,error:String(error),errors},null,2));throw error}
finally{if(browser)await browser.close()}

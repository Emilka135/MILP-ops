/** Actual hosted admin UI + real CRM. All created records are explicitly debug synthetic. */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import {execFileSync} from 'node:child_process';
import {mkdir,writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const out='debug/runs/feedback-module-review/hosted';await mkdir(out,{recursive:true});
const marker='debug-feedback-'+Date.now();
const text='Тестовая жалоба '+marker+': помощник говорит слишком быстро.';
const script="import json,urllib.request; payload="+JSON.stringify(JSON.stringify({call_id:marker,registrar:'100',text,kind:'complaint'}))+"; req=urllib.request.Request('http://127.0.0.1:8080/v1/feedback.submit',data=payload.encode(),headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req).read().decode())";
const seeded=JSON.parse(execFileSync('docker',['compose','exec','-T','crm','python','-c',script],{encoding:'utf8'}));assert.equal(seeded.status,'ok');
const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
const errors=[],checks=[];
try{
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));
 await page.goto('http://127.0.0.1:8090');await page.getByLabel('Пароль администратора').fill('admin');await page.getByRole('button',{name:'Подключиться',exact:true}).click();
 await page.getByRole('button',{name:'Отзывы',exact:true}).click();await page.locator('.feedback-panel').waitFor();
 const card=page.locator('.feedback-complaint').filter({hasText:marker});await card.waitFor();assert.ok(await card.textContent());checks.push('Actual signed CRM dashboard displays the synthetic complaint');
 await page.screenshot({path:out+'/01-admin-complaint.png',fullPage:true});
 await card.getByRole('textbox').fill('Проверено в debug: предложение о темпе зарегистрировано. Автоматическое включение сценария не выполнялось.');await card.getByRole('button',{name:'Сохранить решение',exact:true}).click();
 await page.locator('.feedback-notice').waitFor();assert.equal(await page.locator('.feedback-complaint').filter({hasText:marker}).count(),0);checks.push('Actual complaint resolution persists through signed admin route');
 await page.getByRole('button',{name:'Обновить',exact:true}).click();await page.locator('.feedback-card').filter({hasText:marker}).first().waitFor();checks.push('Reload retains saved resolution');
 await page.screenshot({path:out+'/02-admin-resolved.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'/03-admin-mobile.png',fullPage:true});assert.equal(await page.locator('.feedback-panel').evaluate(el=>el.scrollWidth>el.clientWidth+1),false);checks.push('Mobile panel has no horizontal overflow');
 const denied=await context.request.post('http://127.0.0.1:8090/api/insights/complaint/resolve',{headers:{Origin:'https://foreign.invalid'},data:{id:1,resolution:'blocked'}});assert.equal(denied.status(),403);checks.push('Cross-origin admin write rejected');
 const anonymous=await browser.newContext();assert.equal((await anonymous.request.get('http://127.0.0.1:8090/api/insights/dashboard')).status(),401);checks.push('Anonymous observer cannot read admin feedback');await anonymous.close();
 const staff=await browser.newContext();assert.equal((await staff.request.post('http://127.0.0.1:8090/api/login',{data:{username:'user',password:'user'}})).status(),200);assert.equal((await staff.request.get('http://127.0.0.1:8090/api/insights/dashboard')).status(),403);checks.push('Registrar cannot read admin-only feedback');await staff.close();
 await page.setViewportSize({width:1440,height:1000});await page.goto('http://127.0.0.1:8080/operator');assert.ok(await page.locator('.machine-view').count());await page.screenshot({path:out+'/04-operator-handoff.png',fullPage:false});
 const details=page.locator('details').first();await details.locator('summary').click();assert.ok((await details.textContent()).includes('selected_slot_id'));await page.screenshot({path:out+'/05-operator-expanded.png',fullPage:false});checks.push('Actual operator page opens the frozen query and slot view');await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'/06-operator-mobile.png',fullPage:false});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);checks.push('Mobile operator ticket has no horizontal overflow');
 assert.deepEqual(errors,[]);await writeFile(out+'/result.json',JSON.stringify({status:'passed',scope:'Actual localhost Docker editor and CRM; explicitly synthetic debug complaint',marker,checks,js_errors:errors},null,2));console.log(JSON.stringify({status:'passed',checks:checks.length}));
}finally{await browser.close()}

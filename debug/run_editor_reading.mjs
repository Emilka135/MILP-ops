/** Browser checks for a readable administrative route without modifying scenarios. */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const out=new URL('./runs/editor-reading/',import.meta.url);await mkdir(out,{recursive:true});
const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});const errors=[];let result;
try{
 const page=await browser.newPage({viewport:{width:1600,height:1000}});page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(process.env.EDITOR_URL||'http://127.0.0.1:28490');await page.getByLabel('Пароль администратора').fill('admin');await page.getByRole('button',{name:'Подключиться',exact:true}).click();await page.locator('.react-flow__node').first().waitFor();await page.waitForTimeout(500);
 let count=await page.locator('.react-flow__node').count();assert.ok(count>1&&count<=6,count);await page.screenshot({path:new URL('01-linear-neighbors.png',out).pathname,fullPage:true});
 await page.getByRole('button',{name:'Ветки 1/2',exact:true}).click();await page.waitForTimeout(400);assert.ok(await page.locator('.react-flow__node').count()<=6);await page.screenshot({path:new URL('02-next-alternatives.png',out).pathname,fullPage:true});
 await page.getByLabel('Поиск по всем сценариям').fill('inbound.book_referral_date');await page.locator('.search-result').first().click();await page.getByLabel('Текст сообщения').waitFor();await page.waitForTimeout(400);assert.ok(await page.locator('.react-flow__node').count()<=6);await page.screenshot({path:new URL('03-search-to-readable-step.png',out).pathname,fullPage:true});
 await page.getByRole('button',{name:'Все блоки',exact:true}).click();await page.waitForTimeout(500);assert.ok(await page.locator('.react-flow__node').count()>12);await page.getByRole('button',{name:'Ближайшие шаги',exact:true}).click();await page.waitForTimeout(400);assert.ok(await page.locator('.react-flow__node').count()<=6);
 assert.deepEqual(errors,[]);result={status:'passed',bounded_neighborhood:true,paged_alternatives:true,qualified_search:true,full_graph_available:true,errors};console.log(result)
}catch(e){result={status:'failed',error:String(e),errors};throw e}
finally{await writeFile(new URL('result.json',out),JSON.stringify(result,null,2)+'\n');await browser.close()}

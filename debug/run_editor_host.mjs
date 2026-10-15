/** Inspect the hosted container without changing a working scenario. */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const base=process.env.EDITOR_URL||'http://127.0.0.1:8090',out=new URL('./runs/editor-host/',import.meta.url);
await mkdir(out,{recursive:true});
const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
const errors=[];let report;
try{
 const page=await browser.newPage({viewport:{width:1600,height:1000}});page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(base);await page.getByLabel('Пароль администратора').fill(process.env.EDITOR_PASSWORD||'admin');await page.getByRole('button',{name:'Подключиться',exact:true}).click();await page.locator('.react-flow__node').first().waitFor();await page.waitForTimeout(500);await page.screenshot({path:new URL('01-hosted-constructor.png',out).pathname,fullPage:true});
 await page.getByRole('button',{name:'Аудиофразы',exact:true}).click();await page.locator('.audio-list audio').first().waitFor();
 await page.locator('.audio-list audio').first().evaluate(el=>el.play());await page.waitForFunction(()=>document.querySelector('.audio-list audio')?.currentTime>0);
 await page.screenshot({path:new URL('02-hosted-playback.png',out).pathname,fullPage:true});
 const count=await page.locator('.audio-list article').count();assert.ok(count>0);assert.deepEqual(errors,[]);
 report={status:'passed',base,real_audio_played:true,prompts:count,errors};
 console.log(JSON.stringify(report,null,2));
} catch(error){report={status:'failed',base,error:String(error),errors};throw error}
finally{await writeFile(new URL('result.json',out),JSON.stringify(report,null,2)+'\n');await browser.close()}

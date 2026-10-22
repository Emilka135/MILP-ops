/** Real Chromium UI acceptance with explicit synthetic HTTP fixtures.
 * No speech/ASR, live MIS, model execution or patient calls are claimed here.
 * Run after npm ci: node debug/run_registrar_mis_panel.mjs
 */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import {spawn} from 'node:child_process';
import {mkdir,writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const root=new URL('..',import.meta.url).pathname,out=root+'debug/runs/registrar-mis-panel/';
await mkdir(out,{recursive:true});
const port=Number(process.env.MIS_PANEL_TEST_PORT||5187),base=`http://127.0.0.1:${port}`;
const server=spawn(process.execPath,[root+'scenario-editor/node_modules/vite/bin/vite.js','--host','127.0.0.1','--port',String(port),'--strictPort'],{cwd:root+'scenario-editor',stdio:['ignore','pipe','pipe']});
let serverLog='',browser;server.stdout.on('data',b=>serverLog+=b);server.stderr.on('data',b=>serverLog+=b);
const checks=[],errors=[];
const long='Очень длинное значение '.repeat(80)+'КОНЕЦ';
const makeCall=(id,result=[{full_name:'Фёдоров Пётр Ильич',nullable:null,nested:{поле:'значение'},empty:'',long}])=>({call_id:id,archive_source:'local',created_at:'2026-10-21T12:00:00+03:00',kind:'text',can_propose:false,trace:['demo.start'],graph:{titles:{start:'Проверка записи'},nodes:{start:{id:'demo.start',title:'Проверка записи',lines:['Сохранённая реплика']}},branches:{}},transcript:[{role:'assistant',text:'Сохранённая реплика',step_index:0,state:'demo.start'}],source_data:{mode:'file_snapshot',sources:[]},queries:[{id:'q-'+id,state:'demo.start',step_index:0,sql:'SELECT * FROM doctors WHERE id=:doctor',params:{doctor:42,optional:null,filter:{specialty:'офтальмолог'}},result}]});
const calls={first:makeCall('first'),second:makeCall('second',[{doctor:'Другой врач',value:0,confirmed:false}]),empty:makeCall('empty',[]),missing:makeCall('missing')};delete calls.missing.queries[0].result;
try{
 for(let i=0;i<100;i++){try{if((await fetch(base)).ok)break}catch{}if(server.exitCode!==null)throw Error(serverLog);await new Promise(r=>setTimeout(r,100));if(i===99)throw Error('Vite did not start')}
 browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:1660,height:1050},permissions:['clipboard-read','clipboard-write']});
 const page=await context.newPage();page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(String(e)));
 let slowStarted,releaseSlow;const slowStartedPromise=new Promise(r=>slowStarted=r),slowRelease=new Promise(r=>releaseSlow=r);
 await page.route('**/api/**',async route=>{
  const url=new URL(route.request().url()),path=url.pathname;let data;
  if(path==='/api/me')data={role:'observer',registrar:'100'};
  else if(path==='/api/workshop/calls')data={calls:Object.values(calls),warnings:['Телефонный архив временно недоступен. Сохранённые тестовые разговоры доступны.']};
  else if(path==='/api/workshop/cases')data={cases:[],badges:{},labelled_examples:0};
  else if(path==='/api/workshop/step')data={current:{id:'demo.start',title:'Начало',lines:[]},parents:[],next:[]};
  else if(path==='/api/workshop/call'){const id=url.searchParams.get('id');if(id==='slow'){slowStarted();await slowRelease;data=makeCall('slow')}else data=calls[id]}
  else if(path==='/api/demo-call/start')data={...calls.first,call_id:'live-fixture',state:'demo.start',title:'Текущий звонок',prompt:'Проверочный вопрос',steps:[{title:'Начало'}],finished:false};
  else if(path==='/api/demo-call/end')data={...calls.first,call_id:'live-fixture',state:'demo.closed',finished:true};
  else return route.fulfill({status:404,json:{error:'Unknown fixture route '+path}});
  await route.fulfill({json:data});
 });
 await page.goto(base+'/registrar');
 const inspector=page.getByRole('region',{name:'Ответы МИС и тестовой базы'});
 await inspector.waitFor();assert.ok(!(await page.locator('.recent-calls').innerText()).includes('undefined'));await page.getByText(/Телефонный архив временно недоступен/).waitFor();checks.push('Archive warnings remain visible while saved calls are usable');
 async function open(id){await page.getByLabel('Номер звонка').fill(id);await page.getByRole('button',{name:'Открыть звонок',exact:true}).click();await inspector.locator('.query-call').filter({hasText:'Звонок: '+id}).waitFor()}
 await open('first');
 const mainBox=await page.locator('.quest-main').boundingBox(),paneBox=await page.locator('.query-sidebar').boundingBox();assert.ok(paneBox.x>=mainBox.x+mainBox.width);checks.push('Archive response panel is to the right of the call');
 assert.ok((await inspector.innerText()).includes('Тестовая SQLite'));assert.ok(!(await inspector.innerText()).includes('LLM SQL'));assert.ok((await inspector.innerText()).includes('Сверка с текущими данными не выполнялась'));
 const btn=name=>inspector.getByRole('button',{name,exact:true});
 await btn('Копировать full_name, строка 1').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'Фёдоров Пётр Ильич');
 await btn('Копировать nullable, строка 1').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'NULL');
 await btn('Копировать nested, строка 1').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'{"поле":"значение"}');
 await btn('Копировать empty, строка 1').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'');
 await btn('Копировать long, строка 1').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),long);checks.push('Actual browser clipboard contains exact FIO, NULL, JSON, empty and long values');
 await inspector.getByText('Запрос и параметры',{exact:true}).click();await btn('Копировать значение, строка 2').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'NULL');
 await btn('Копировать текст запроса').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),calls.first.queries[0].sql);checks.push('Every parameter and SQL text has a working copy control');
 await inspector.locator('.query-results').first().evaluate(el=>el.scrollLeft=0);
 await page.evaluate(()=>window.scrollTo(0,0));
 const splitter=page.getByLabel('Ширина панели базы данных'),before=Number(await splitter.getAttribute('aria-valuenow')),box=await splitter.boundingBox();await page.mouse.move(box.x+3,box.y+70);await page.mouse.down();await page.mouse.move(box.x-100,box.y+70,{steps:8});await page.mouse.up();assert.ok(Number(await splitter.getAttribute('aria-valuenow'))>before+70);await splitter.focus();await page.keyboard.press('Home');assert.equal(Number(await splitter.getAttribute('aria-valuenow')),260);await splitter.dblclick();checks.push('Right panel resizes with real pointer and keyboard');
 await page.screenshot({path:out+'01-right-panel-archive.png',fullPage:true});
 // Exercise the actual legacy browser copy operation after Clipboard API denial.
 await page.evaluate(()=>{window.savedClipboard=navigator.clipboard;Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(Error('Denied'))}})});
 await btn('Копировать full_name, строка 1').click();assert.equal(await page.evaluate(()=>window.savedClipboard.readText()),'Фёдоров Пётр Ильич');checks.push('Clipboard API denial falls back to actual execCommand copy');
 await page.evaluate(()=>{window.savedExec=document.execCommand;document.execCommand=()=>false});await btn('Копировать full_name, строка 1').click();await inspector.getByRole('alert').filter({hasText:'Не удалось скопировать'}).waitFor();checks.push('Clipboard failure gives visible manual-copy instruction');
 await page.evaluate(()=>{document.execCommand=window.savedExec;Object.defineProperty(navigator,'clipboard',{configurable:true,value:window.savedClipboard})});
 await open('second');assert.equal(await inspector.getByRole('alert').count(),0);assert.ok(!(await inspector.innerText()).includes('Фёдоров'));checks.push('Changing calls clears previous result and clipboard feedback');
 await open('empty');await inspector.getByText('Запрос вернул пустой список. Совпадений нет.').waitFor();
 await open('missing');await inspector.getByText('Результат запроса не сохранён.').waitFor();checks.push('Empty query and missing result are distinguished');
 await page.getByLabel('Номер звонка').fill('slow');await page.getByRole('button',{name:'Открыть звонок',exact:true}).click();await slowStartedPromise;await open('second');releaseSlow();await page.waitForTimeout(300);assert.equal(await inspector.locator('.query-call').innerText(),'Звонок: second');checks.push('Late archive response cannot replace the newer selection');
 await page.getByRole('button',{name:'Начать пробный звонок',exact:true}).click();await inspector.locator('.query-call').filter({hasText:'live-fixture'}).waitFor();assert.ok((await inspector.innerText()).includes('Фёдоров'));checks.push('Live onStep response populates the same right panel');
 await page.screenshot({path:out+'02-right-panel-live.png',fullPage:true});
 await page.setViewportSize({width:900,height:900});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));const mediumMain=await page.locator('.quest-main').boundingBox(),mediumPane=await page.locator('.query-sidebar').boundingBox();assert.ok(mediumPane.x>=mediumMain.x+mediumMain.width);checks.push('900px layout keeps the response adjacent without page overflow');
 await page.setViewportSize({width:390,height:844});await inspector.scrollIntoViewIfNeeded();
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'Narrow page must not overflow horizontally');
 await inspector.getByRole('button',{name:'Копировать full_name, строка 1',exact:true}).click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'Фёдоров Пётр Ильич');
 await page.screenshot({path:out+'03-narrow-panel.png',fullPage:true});checks.push('390px screen has no page overflow and supports copying');
 assert.deepEqual(errors,[]);
 const result={status:'passed',scope:'Real Chromium and real clipboard; synthetic HTTP fixtures; no MIS/ASR/model execution',checks,errors};await writeFile(out+'result.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result,null,2));
}catch(error){if(browser)await browser.contexts()[0]?.pages()[0]?.screenshot({path:out+'failure.png',fullPage:true}).catch(()=>{});await writeFile(out+'result.json',JSON.stringify({status:'failed',error:String(error),checks,errors},null,2));throw error}
finally{await browser?.close();server.kill();await writeFile(out+'vite.log',serverLog)}

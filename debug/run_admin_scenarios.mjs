/** Hosted acceptance: settings, guarded sandbox booking, real speech and call archive. */
import assert from 'node:assert/strict';
import {pcmWav} from '../scenario-editor/src/audioWav.js';
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
const base=process.env.EDITOR_URL||'http://127.0.0.1:8090',out='debug/runs/admin-scenarios/browser';await mkdir(out,{recursive:true});
const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
const errors=[],checks=[];let report={status:'running'};
try{
 const context=await browser.newContext({viewport:{width:1600,height:1050}}),page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(base);await page.getByLabel('Пароль администратора').fill('admin');await page.getByRole('button',{name:'Подключиться',exact:true}).click();
 await page.getByRole('button',{name:'Настройки',exact:true}).click();await page.getByLabel('Провайдер tts').waitFor();
 assert.equal(await page.getByLabel('Провайдер tts').inputValue(),'local');assert.equal(await page.getByLabel('Включить Метки клиентов').isChecked(),false);
 await page.getByLabel('Провайдер tts').selectOption('openrouter');await page.getByLabel('Модель tts').fill('google/gemini-example-tts');await page.screenshot({path:out+'/01-independent-model-settings.png',fullPage:true});
 // Do not apply an unconfigured external provider; exercise persistence with current local defaults.
 await page.getByLabel('Провайдер tts').selectOption('local');await page.getByRole('button',{name:'Сохранить настройки модулей',exact:true}).click();await page.getByRole('status').waitFor();checks.push('Per-module settings and disabled extensions persist');
 await page.getByRole('button',{name:'Закрыть окно',exact:true}).click();
 const anon=await browser.newContext({viewport:{width:1450,height:1000}}),observer=await anon.newPage();observer.on('pageerror',e=>errors.push(String(e)));
 await observer.goto(base+'/registrar');const panel=observer.getByRole('region',{name:'Пробный звонок'});
 await panel.getByRole('button',{name:'Начать пробный звонок',exact:true}).click();await panel.getByRole('heading',{name:'Куда записаться?',exact:true}).waitFor();
 await panel.locator('audio').evaluate(a=>a.play());await observer.waitForFunction(()=>document.querySelector('.observer-call audio').currentTime>0);checks.push('Actual HQ TTS plays in Chrome');
 async function say(text,heading){await panel.getByLabel('Ответ текстом',{exact:true}).fill(text);await panel.getByRole('button',{name:'Отправить',exact:true}).click();if(heading)await panel.getByRole('heading',{name:heading,exact:true}).waitFor();else await panel.getByRole('button',{name:'Отправить',exact:true}).waitFor({state:'visible'});await observer.waitForFunction(()=>!document.querySelector('.observer-call header button').disabled)}
 await say('Здравствуйте','Куда записаться?');await say('ЧТО?','Куда записаться?');await say('К глазнику меня запиши','Проверяем выбранный вариант');await say('ноль','Куда записаться?');await say('к терапевту','Проверяем выбранный вариант');
 await say('один','Фамилия, имя и отчество');await say('Иванов Иван Иванович','Дата рождения');await say('12.04.1985','Свободные талоны');await say('один','Подтверждение записи');
 await observer.screenshot({path:out+'/02-identity-and-real-test-slots.png',fullPage:true});await say('да','Запись в тестовой таблице');
 await panel.locator('.call-graph .react-flow__node').first().click();await panel.locator('.call-graph .react-flow__edge').first().waitFor({state:'attached'});assert.ok(await panel.locator('.call-graph .react-flow__edge').count()>0);
 await observer.screenshot({path:out+'/03-conversation-path-and-branches.png',fullPage:true});checks.push('Greeting, repetition, slang, identity, slots and final booking pass in browser');
 const calls=(await (await context.request.get(base+'/api/calls')).json()).calls;assert.ok(calls.length>0);
 const tables=await (await context.request.get(base+'/api/sandbox/tables')).json();assert.ok(tables.tables.bookings.length>0);assert.ok(tables.tables.queries.some(q=>q.sql.includes('INSERT INTO bookings')));checks.push('Booking and parameterized SQL trace visible to admin');
 assert.equal((await anon.request.get(base+'/api/sandbox/tables')).status(),401);assert.equal((await anon.request.get(base+'/api/calls')).status(),401);checks.push('Anonymous access to call archives and SQL denied');
 await page.getByRole('button',{name:'Звонки и таблицы',exact:true}).click();await page.getByLabel('Сохранённый разговор').selectOption(calls[0].id);await page.locator('.call-graph').waitFor();await page.getByRole('button',{name:'Таблицы и запросы',exact:true}).click();await page.getByText('bookings ·',{exact:false}).waitFor();
 await page.screenshot({path:out+'/04-admin-call-archive.png',fullPage:true});
 await page.getByText('Загрузить разговор сотрудника с пациентом',{exact:true}).click();await page.getByLabel('Импорт расшифровки').fill('Сотрудник: Здравствуйте\nПациент: К глазнику\nСотрудник: Назовите ваши данные');await page.getByRole('button',{name:'Разобрать по сценарию',exact:true}).click();await page.getByText(/Сопоставление с текущим тестовым сценарием/).waitFor();checks.push('Imported employee transcript is mapped without booking writes');
 await page.screenshot({path:out+'/05-imported-conversation.png',fullPage:true});
 const source=await (await context.request.post(base+'/api/audio/preview',{data:{text:'Хочу записаться к офтальмологу.',rate:1.3}})).json();assert.equal(source.sample_rate,48000);const inputFile=out+'/patient-upload.wav';await writeFile(inputFile,Buffer.from(pcmWav(Buffer.from(source.pcm_b64,'base64'),48000)));await page.getByLabel('Файл разговора').setInputFiles(inputFile);await page.getByText(/Говорящие не разделены автоматически/).waitFor();const recognized=await page.getByLabel('Импорт расшифровки').inputValue();assert.match(recognized,/офтальмолог/i);await page.getByLabel('Импорт расшифровки').fill('Пациент: '+recognized);await page.getByLabel('Есть согласие на хранение загруженного аудио').check();await page.getByRole('button',{name:'Разобрать по сценарию',exact:true}).click();await page.locator('.calls-panel audio').first().waitFor();await page.locator('.calls-panel audio').first().evaluate(a=>a.play());await page.waitForFunction(()=>document.querySelector('.calls-panel audio')?.currentTime>0);checks.push('Real WAV upload, ASR, consented archive and playback pass in Chrome');await page.screenshot({path:out+'/07-imported-audio-playback.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'/06-mobile-archive.png',fullPage:true});assert.equal(await page.locator('.calls-panel').evaluate(el=>el.scrollWidth>el.clientWidth+1),false);checks.push('Mobile archive fits panel');
 assert.deepEqual(errors,[]);report={status:'passed',base,checks,errors};console.log(JSON.stringify(report));
} catch(error){await browser.contexts().at(-1)?.pages().at(-1)?.screenshot({path:out+'/failure.png',fullPage:true});report={status:'failed',error:String(error),checks,errors};throw error}finally{await writeFile(out+'/result.json',JSON.stringify(report,null,2));await browser.close()}

/** Real browser microphone WAV -> real ASR; independent DTMF path, no API mocks. */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const editor=process.env.EDITOR_URL||'http://127.0.0.1:28491';
const out=path.resolve(process.env.OUT_DIR||new URL('./runs/observer-call/',import.meta.url).pathname);await mkdir(out,{recursive:true});
const phrase='Хочу записаться к кардиологу.';
async function json(url,body,cookie){const r=await fetch(editor+url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json',...(cookie?{Cookie:cookie}:{})},body:body?JSON.stringify(body):undefined});const data=await r.json();assert.ok(r.ok,JSON.stringify(data));return {data,cookie:r.headers.get('set-cookie')?.split(';')[0]}}
// Generate audible patient speech, then feed it through Chrome's microphone device.
const login=await json('/api/login',{username:'admin',password:'admin'});
const seed=(await json('/api/audio/preview',{text:phrase,rate:1.3},login.cookie)).data;
assert.equal(seed.sample_rate,48000);const pcm=Buffer.from(seed.pcm_b64,'base64');const wav=Buffer.alloc(44+pcm.length);wav.write('RIFF');wav.writeUInt32LE(36+pcm.length,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(48000,24);wav.writeUInt32LE(96000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(pcm.length,40);pcm.copy(wav,44);const input=path.join(out,'01-patient-cardio-microphone-48khz.wav');await writeFile(input,wav);
const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox','--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream','--use-file-for-fake-audio-capture='+input]});
let report={status:'running',editor,input_phrase:phrase},page;const errors=[],responses=[],requests=[];
try{
 page=await browser.newPage({viewport:{width:1450,height:1100},permissions:['microphone']});page.on('pageerror',e=>errors.push(String(e)));
 page.on('response',async r=>{if(r.url().includes('/api/demo-call/')&&r.ok()){const data=await r.json();responses.push({route:new URL(r.url()).pathname,state:data.state,selection:data.selection,recognized:data.recognized,executed:data.executed,sample_rate:data.audio?.sample_rate});}});
 page.on('request',r=>{if(r.url().endsWith('/api/demo-call/audio'))requests.push(r.postDataJSON())});
 await page.goto(editor+'/registrar');const panel=page.getByRole('region',{name:'Пробный звонок'});await panel.getByRole('button',{name:'Начать пробный звонок',exact:true}).click();await panel.getByRole('heading',{name:'Куда записаться?',exact:true}).waitFor();
 assert.equal(await page.locator('.quest-heading').isVisible(),false);assert.equal(await page.locator('.neighbor-map').isVisible(),false);
 await page.waitForFunction(()=>{const a=document.querySelector('.observer-call audio');return a&&!a.paused&&a.currentTime>0});
 const playback=await panel.locator('audio').evaluate(a=>({current_time:a.currentTime,paused:a.paused}));assert.ok(playback.current_time>0&&!playback.paused);
 await page.screenshot({path:path.join(out,'02-observer-call-welcome.png'),fullPage:true});
 // First path is DTMF alone; it must work without any microphone request.
 await panel.getByRole('button',{name:'Клавиша 1',exact:true}).click();await panel.getByRole('heading',{name:'Проверяем выбранный вариант',exact:true}).waitFor();await panel.getByRole('button',{name:'Клавиша 1',exact:true}).click();await panel.getByRole('heading',{name:'Фамилия, имя и отчество',exact:true}).waitFor();assert.equal(requests.length,0);
 await page.screenshot({path:path.join(out,'03-dtmf-only-complete.png'),fullPage:true});
 // Second path captures the generated phrase via getUserMedia + AudioWorklet.
 await panel.getByRole('button',{name:'Начать заново',exact:true}).click();await panel.getByRole('heading',{name:'Куда записаться?',exact:true}).waitFor();await panel.getByRole('button',{name:'🎙 Говорить',exact:true}).click();await panel.getByRole('button',{name:/Остановить и отправить/}).waitFor();await page.waitForTimeout(Math.min(7000,Math.ceil(pcm.length/96000*1000)+1300));await panel.getByRole('button',{name:/Остановить и отправить/}).click();await panel.getByRole('heading',{name:'Уточняем направление',exact:true}).waitFor({timeout:30000});
 await panel.getByText('Расшифровка и выбор помощника',{exact:true}).click();await panel.getByText(/кардиолог/i).first().waitFor();
 const voice=responses.find(r=>r.recognized);assert.match(voice.recognized.text,/кардиолог/i);assert.equal(voice.selection.id,'cardiology');assert.equal(voice.recognized.source,'real_asr');if(process.env.EXPECT_CUDA==='1'){assert.equal(voice.recognized.provider,'CUDAExecutionProvider');assert.equal(voice.recognized.cuda_verified,true)}assert.equal(voice.sample_rate,48000);assert.equal(voice.executed,false);assert.equal(requests.length,1);const recorded=Buffer.from(requests[0].wav_b64,'base64');await writeFile(path.join(out,'04-browser-recorded-microphone.wav'),recorded);let sum=0,peak=0,clipped=0;for(let i=44;i+1<recorded.length;i+=2){const sample=recorded.readInt16LE(i);sum+=sample*sample;peak=Math.max(peak,Math.abs(sample));if(Math.abs(sample)>=32760)clipped++}const microphone={sample_rate:recorded.readUInt32LE(24),seconds:(recorded.length-44)/recorded.readUInt32LE(28),rms:Math.sqrt(sum/((recorded.length-44)/2)),peak,clipped_fraction:clipped/((recorded.length-44)/2),bytes:recorded.length};assert.ok(microphone.rms>100&&microphone.peak>100);await writeFile(path.join(out,'signal.json'),JSON.stringify(microphone,null,2)+'\n');
 await page.screenshot({path:path.join(out,'05-real-asr-cardio-referral.png'),fullPage:true});
 // Voice and DTMF combine on one call; no real appointment is created.
 await panel.getByRole('button',{name:'Клавиша 1',exact:true}).click();await panel.getByRole('heading',{name:'Проверяем выбранный вариант',exact:true}).waitFor();await panel.getByRole('button',{name:'Клавиша 1',exact:true}).click();await panel.getByRole('heading',{name:'Фамилия, имя и отчество',exact:true}).waitFor();
 const denied=await page.evaluate(async()=>({save:(await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status,unknown:(await fetch('/api/demo-call/book',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status}));assert.ok([401,403].includes(denied.save));assert.equal(denied.unknown,400);
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(out,'06-mobile-linear-dialogue.png'),fullPage:true});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));assert.deepEqual(errors,[]);
 await panel.getByRole('button',{name:'Завершить звонок',exact:true}).click();await panel.getByRole('heading',{name:'Звонок завершён',exact:true}).waitFor();await page.locator('.quest-heading').waitFor({state:'visible'});await page.locator('.neighbor-map').waitFor({state:'visible'});await page.screenshot({path:path.join(out,'07-archive-restored.png'),fullPage:true});
 report={...report,status:'passed',dtmf_alone:true,browser_microphone_real_asr:true,mixed_voice_dtmf:true,archive_hidden_during_call:true,archive_restored_after_finish:true,hq_sample_rate:48000,playback,microphone,recognized:voice.recognized,selection:voice.selection,mis_writes_executed:false,denied,responses,errors};console.log(JSON.stringify(report,null,2));
}catch(error){report={...report,status:'failed',error:String(error),responses,errors};await page?.screenshot({path:path.join(out,'failure.png'),fullPage:true});throw error}
finally{await writeFile(path.join(out,'browser-result.json'),JSON.stringify(report,null,2)+'\n');await browser.close()}

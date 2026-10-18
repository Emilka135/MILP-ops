/** Voice-only sandbox conversation: real HQ TTS -> WAV -> real ASR, no DTMF. */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const editor=process.env.EDITOR_URL||'http://127.0.0.1:28491',out=path.resolve(process.env.OUT_DIR||'debug/runs/observer-call/voice-only');await mkdir(out,{recursive:true});
async function api(route,body,cookie){const response=await fetch(editor+'/api/'+route,{method:'POST',headers:{'Content-Type':'application/json',...(cookie?{Cookie:cookie}:{})},body:JSON.stringify(body)});const data=await response.json();assert.ok(response.ok,JSON.stringify(data));return {data,cookie:response.headers.get('set-cookie')?.split(';')[0]}}
const auth=await api('login',{username:'admin',password:'admin'});const started=(await api('demo-call/start',{})).data;const turns=[];let report={editor,method:'real_tts_wav_to_real_asr_no_dtmf',call_id:started.call_id};
try{for(const [index,text] of ['Хочу записаться к кардиологу.','Да, конечно.','Да, конечно.'].entries()){
 const voice=(await api('audio/preview',{text,rate:1.3},auth.cookie)).data;assert.equal(voice.sample_rate,48000);const pcm=Buffer.from(voice.pcm_b64,'base64'),wav=Buffer.alloc(44+pcm.length);wav.write('RIFF');wav.writeUInt32LE(36+pcm.length,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(48000,24);wav.writeUInt32LE(96000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(pcm.length,40);pcm.copy(wav,44);await writeFile(path.join(out,`${index+1}-patient.wav`),wav);
 const result=(await api('demo-call/audio',{call_id:started.call_id,wav_b64:wav.toString('base64')})).data;assert.equal(result.recognized.source,'real_asr');if(process.env.EXPECT_CUDA==='1'){assert.equal(result.recognized.provider,'CUDAExecutionProvider');assert.equal(result.recognized.cuda_verified,true)}assert.equal(result.executed,false);assert.equal(result.audio.sample_rate,48000);turns.push({input:text,recognized:result.recognized,state:result.state,selection:result.selection,executed:result.executed});
 assert.equal(result.state,['demo.referral','demo.confirm','demo.done'][index]);
 }report={...report,status:'passed',turns,dtmf_requests:0,mis_writes_executed:false};console.log(JSON.stringify(report,null,2));
}catch(error){report={...report,status:'failed',error:String(error),turns};throw error}finally{await api('demo-call/end',{call_id:started.call_id});await writeFile(path.join(out,'result.json'),JSON.stringify(report,null,2)+'\n')}

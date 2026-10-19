import React,{useEffect,useState} from 'react';
import CallGraph from './CallGraph';
import {base64,microphoneWav} from './audioWav';
export default function CallsPanel({api}){
 const [calls,setCalls]=useState([]),[call,setCall]=useState(null),[tables,setTables]=useState(null),[text,setText]=useState(''),[upload,setUpload]=useState(''),[progress,setProgress]=useState(''),[consent,setConsent]=useState(false),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 async function refresh(){setCalls((await api('calls')).calls)}
 useEffect(()=>{refresh().catch(e=>setError(e.message))},[]);
 async function run(fn){setBusy(true);setError('');try{await fn()}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function file(f){if(!f)return;await run(async()=>{
  if(upload){await api('calls/upload',{op:'cancel',upload_id:upload});setUpload('')}
  if(f.name.toLowerCase().endsWith('.wav')){
   if(f.size>128*1024*1024)throw Error('WAV должен быть до 128 MiB и десяти минут');
   const context=new AudioContext({sampleRate:48000});let decoded;
   try{decoded=await context.decodeAudioData(await f.arrayBuffer())}finally{await context.close()}
   if(decoded.duration>600)throw Error('Разговор должен быть до десяти минут');
   const mono=new Float32Array(decoded.length);
   for(let channel=0;channel<decoded.numberOfChannels;channel++){const part=decoded.getChannelData(channel);for(let i=0;i<mono.length;i++)mono[i]+=part[i]/decoded.numberOfChannels}
   const id=(await api('calls/upload',{op:'start'})).upload_id;setUpload(id);
   try{let index=0;const size=decoded.sampleRate*10;
    for(let at=0;at<mono.length;at+=size){setProgress('Распознаём фрагмент '+(index+1));const wav=microphoneWav([mono.slice(at,at+size)],decoded.sampleRate);const d=await api('calls/upload',{op:'chunk',upload_id:id,index:index++,wav_b64:base64(wav)});setText(d.text)}
    setProgress('Говорящие не разделены автоматически. Разметьте строки «Пациент: …» и «Сотрудник: …».');
   }catch(e){await api('calls/upload',{op:'cancel',upload_id:id}).catch(()=>{});setUpload('');throw e}
  }else{if(f.size>100000)throw Error('Расшифровка должна быть до 100000 символов');setText(await f.text());setProgress('')}
 })}

 return <div className="calls-panel">{error&&<p role="alert" className="module-error">{error}</p>}<div className="module-toolbar"><button className="secondary" onClick={()=>run(refresh)}>Обновить звонки</button><button className="secondary" onClick={()=>run(async()=>setTables(await api('sandbox/tables')))}>Таблицы и запросы</button></div><label>Сохранённый разговор<select aria-label="Сохранённый разговор" value={call?.call_id||''} onChange={e=>e.target.value&&run(async()=>setCall(await api('calls/detail?id='+encodeURIComponent(e.target.value))))}><option value="">Выберите звонок</option>{calls.map(c=><option value={c.id} key={c.id}>{new Date(c.created_at).toLocaleString('ru-RU')} · {c.id.slice(0,12)}</option>)}</select></label><details><summary>Загрузить разговор сотрудника с пациентом</summary><p>Текст: по одной реплике на строку с меткой «Пациент:» или «Сотрудник:». WAV: до десяти минут и 128 MiB. После ASR разметьте говорящих. Сопоставление не создаёт запись на приём.</p><input aria-label="Файл разговора" type="file" accept=".txt,.wav" disabled={busy} onChange={e=>file(e.target.files[0])}/><textarea aria-label="Импорт расшифровки" rows={7} value={text} onChange={e=>setText(e.target.value)} placeholder={'Пациент: Здравствуйте\nСотрудник: К какому врачу хотите записаться?\nПациент: К глазнику'}/><label><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/> Есть согласие на хранение загруженного аудио</label><button className="primary" disabled={busy||!text.trim()} onClick={()=>run(async()=>{setCall(await api('calls/import',{text,consent_audio:consent,...(upload?{upload_id:upload}:{})}));setUpload('');await refresh()})}>Разобрать по сценарию</button></details>{(busy||progress)&&<p role="status">{progress||'Обрабатываем…'}</p>}{call&&<><p><strong>{call.title}</strong> · {call.proposed_action}</p><p>Причины передачи / флаги: {call.flags?.join(', ')||'нет'}</p>{call.analysis_note&&<p>{call.analysis_note}</p>}<CallGraph key={call.call_id} call={call}/>{call.mapping?.some(m=>m.status==='needs_review')&&<p>Некоторые реплики требуют ручной проверки сопоставления.</p>}{call.audio_files?.map(file=><label key={file}>{file}<audio controls preload="none" src={'/api/calls/audio?id='+encodeURIComponent(call.call_id)+'&file='+encodeURIComponent(file)}/></label>)}<details><summary>Полная расшифровка и разбор</summary>{call.transcript?.map((t,i)=><p key={i}><strong>{t.role}:</strong> {t.text}</p>)}<pre>{JSON.stringify(call.mapping||call.sandbox_booking,null,2)}</pre></details></>}{tables&&<details open><summary>Тестовая база</summary><p>{tables.source}</p>{Object.entries(tables.tables).map(([name,rows])=><details key={name}><summary>{name} · {rows.length}</summary><div className="query-table"><table><thead><tr>{Object.keys(rows[0]||{}).map(k=><th key={k}>{k}</th>)}</tr></thead><tbody>{rows.map((r,i)=><tr key={i}>{Object.values(r).map((v,j)=><td key={j}>{String(v??'')}</td>)}</tr>)}</tbody></table></div></details>)}</details>}</div>
}

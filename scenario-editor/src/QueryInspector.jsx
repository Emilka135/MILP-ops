import React,{useState} from 'react';
import {sourceMoment} from './sourceMoment';
import {CopyValue,QueryAnswer} from './QueryData';

export default function QueryInspector({call}){
 // Remount the selection together with clipboard feedback when changing calls.
 return <Inspector key={(call?.archive_source||'live')+':'+(call?.call_id||'none')} call={call}/>;
}
function Inspector({call}){
 const queries=call?.queries||[],[selection,setSelection]=useState(null);
 // Follow newly completed queries until the registrar explicitly selects one.
 const query=queries.find(q=>String(q.id)===selection)||queries.at(-1);
 const source=call?.source_data;
 const origin=source?.mode==='file_snapshot'?'Тестовая SQLite · снимок исходных CSV и Excel':source?.mode==='sandbox'||call?.archive_source==='local'||source?.mode==='synthetic'?'Изолированная тестовая SQLite':source?.mode?`Источник из журнала: ${source.mode}`:'Источник данных не указан в журнале';
 return <section className="query-inspector" aria-label="Ответы МИС и тестовой базы"><h2>Ответы МИС и тестовой базы</h2><p className="query-origin">{origin}</p>{call?.source_data?.sources&&<details><summary>Источники данных</summary>{source.sources.map((s,i)=><article key={s.kind+':'+i}><CopyValue value={s.path} label="путь источника"/><small>Загружено: {sourceMoment(s.imported_at)} · {s.row_count} строк</small><CopyValue value={s.sha256} label="SHA256 источника"/></article>)}{source.limitations?.map(note=><p key={note}>{note}</p>)}</details>}{source?.reference_sources?.length>0&&<details><summary>Справочные сайты</summary>{source.reference_sources.map(s=><article key={s.location}><a href={s.location.startsWith('http')?s.location:undefined} target="_blank" rel="noopener noreferrer">{s.name}</a><small> · Проверено {sourceMoment(s.checked_at)}</small></article>)}</details>}{call&&<p className="query-call">Звонок: {call.call_id}</p>}{query?<><label>Выполненные запросы<select aria-label="Выполненные запросы" value={String(query.id)} onChange={e=>setSelection(e.target.value)}>{queries.map((q,i)=><option key={q.id} value={String(q.id)}>{i+1}. Шаг {(q.step_index??0)+1} · {q.state}</option>)}</select></label><button className="query-follow" type="button" onClick={()=>setSelection(null)} disabled={selection===null}>Показать последний запрос</button><QueryAnswer key={query.id} query={query}/></>:<p>Запросов пока нет. Начните звонок или откройте сохранённый разговор. Здесь появится фактический сохранённый ответ.</p>}</section>
}

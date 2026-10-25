import React,{useEffect,useState} from 'react';
import {Play,Code,Database} from 'lucide-react';
import {workbench} from './clinicApi';
import {DataTable,CopyValue} from './QueryData';
export default function ManualConsole({mode,identity}){
 const sql=mode==='sql',staff=identity?.role!=='observer';
 const [connections,setConnections]=useState(null),[text,setText]=useState(sql?'SELECT id, full_name, birth_date FROM patients LIMIT 20':'{}'),[params,setParams]=useState('[]'),[path,setPath]=useState('/doctors'),[method,setMethod]=useState('GET'),[confirmed,setConfirmed]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[result,setResult]=useState(null);
 useEffect(()=>{workbench.connections().then(setConnections).catch(e=>setError(e.message))},[]);
 async function run(e){e.preventDefault();setError('');setBusy(true);try{const value=sql?await workbench.sql(text,JSON.parse(params),confirmed):await workbench.request(method,path,method==='GET'?JSON.parse(text):{},method==='GET'?undefined:JSON.parse(text),confirmed);setResult(value);setConfirmed(false)}catch(e){setError(e.message)}finally{setBusy(false)}}
 const connection=connections?.[sql?'database':'mis'];
 const rows=Array.isArray(result?.result)?result.result:result&&result.result!==undefined?[result.result]:[];
 return <section className="manual-console" aria-label={sql?'Редактор SQL':'Ручные запросы API'}><header className="section-heading"><h1>{sql?<><Database size={17}/>Редактор SQL</>:<><Code size={17}/>Ручные API</>}</h1><span>{connection?.label||'Подключение…'}</span></header><p className="manual-source">{connection?.reference?'Референсный сервис · тестовые данные':'Настроенное подключение'}{connection?.base_url&&<> · <CopyValue value={connection.base_url} label="адрес сервиса"/></>}</p>
 <form onSubmit={run}>{!sql&&<div className="manual-url"><select aria-label="Метод API" value={method} onChange={e=>setMethod(e.target.value)}>{['GET','POST','PATCH','DELETE'].map(m=><option key={m}>{m}</option>)}</select><input aria-label="Путь API" value={path} onChange={e=>setPath(e.target.value)}/></div>}
 <label>{sql?'SQL-запрос':method==='GET'?'Параметры запроса · JSON':'Тело запроса · JSON'}<textarea spellCheck={false} aria-label={sql?'SQL-запрос':'Параметры API'} rows={8} value={text} onChange={e=>setText(e.target.value)}/></label>
 {sql&&<label>Параметры SQL · JSON-массив<input aria-label="Параметры SQL" value={params} onChange={e=>setParams(e.target.value)}/></label>}
 {staff&&<label className="manual-consent"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>Подтверждаю изменение данных этим запросом</label>}
 <button type="submit" className="quest-primary" disabled={busy}><Play size={13}/>{busy?'Выполняется…':'Выполнить'}</button></form>
 {error&&<p role="alert" className="quest-error">{error}</p>}{result&&<><div className="manual-result-status" role="status">{result.source} · {result.duration_ms} мс · {result.changed_rows?'Изменено строк: '+result.changed_rows:'Получено строк: '+rows.length}{result.truncated?' · показаны первые 200':''}</div>{rows.length?<DataTable rows={rows} label="Результат ручного запроса"/>:<p>Пустой результат</p>}<details><summary>JSON и сведения о запросе</summary><CopyValue value={result} label="ответ ручного запроса"/></details></>}
 </section>
}

import React,{useRef,useState} from 'react';
import './QueryData.css';

export const displayValue=value=>value===undefined?'— (поле отсутствует)':value===null?'NULL':typeof value==='object'?JSON.stringify(value):String(value);

// Keep the fallback inside the user gesture. A rejected Clipboard API call may
// still be recoverable in older browsers; a failed fallback must stay visible.
export async function copyValue(text){
 try{if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(text);return}}catch{}
 const field=document.createElement('textarea');field.value=text;field.setAttribute('readonly','');field.setAttribute('aria-hidden','true');field.style.cssText='position:fixed;left:-9999px;top:0';
 const focused=document.activeElement;document.body.appendChild(field);field.select();
 try{if(!document.execCommand('copy'))throw Error('Копирование недоступно')}finally{field.remove();focused?.focus()}
}
export function CopyValue({value,label='значение'}){
 const [status,setStatus]=useState(''),attempt=useRef(0),text=displayValue(value);
 async function copy(){const id=++attempt.current;try{await copyValue(text);if(id===attempt.current)setStatus('Скопировано')}catch{if(id===attempt.current)setStatus('Не удалось скопировать. Выделите значение и скопируйте вручную.')}}
 return <div className="copy-value"><span className="copy-text">{text===''?'(пустая строка)':text}</span><button type="button" aria-label={'Копировать '+label} title={'Копировать '+label} onClick={copy}>Копировать</button>{status&&<small role={status==='Скопировано'?'status':'alert'}>{status}</small>}</div>
}
export function DataTable({rows,label='Ответ запроса'}){
 const normalized=rows.map(row=>row!==null&&typeof row==='object'&&!Array.isArray(row)?row:{значение:row});
 const columns=[...new Set(normalized.flatMap(row=>Object.keys(row)))];
 return <div className="query-results" tabIndex={0} aria-label={label}><table style={{minWidth:Math.max(300,columns.length*170)}}><thead><tr>{columns.map(c=><th key={c} scope="col">{c}</th>)}</tr></thead><tbody>{normalized.map((row,i)=><tr key={i}>{columns.map(c=><td key={c}><CopyValue key={displayValue(row[c])} value={row[c]} label={`${c}, строка ${i+1}`}/></td>)}</tr>)}</tbody></table></div>
}
export function QueryAnswer({query}){
 const hasResult=Object.hasOwn(query,'result'),result=query.result,rows=Array.isArray(result)?result:hasResult?[result]:[];
 return <>{query.error?<div role="alert">Запрос завершился ошибкой: <CopyValue value={query.error} label="ошибку запроса"/></div>:<p className="query-verification">{hasResult?'Ответ сохранён в журнале звонка.':'Ответ в журнале отсутствует.'} Сверка с текущими данными не выполнялась.</p>}{hasResult?<><p>Результат: {rows.length} {rows.length===1?'строка':'строк'}</p>{rows.length?<DataTable rows={rows}/>:<p className="query-empty">Запрос вернул пустой список. Совпадений нет.</p>}</>:<p>Результат запроса не сохранён.</p>}<details className="query-technical"><summary>Запрос и параметры</summary><p>Запрос</p><CopyValue value={query.sql} label="текст запроса"/><p>Параметры</p>{query.params!==null&&typeof query.params==='object'?<DataTable rows={Array.isArray(query.params)?query.params.map((value,index)=>({параметр:index+1,значение:value})):Object.entries(query.params).map(([name,value])=>({параметр:name,значение:value}))} label="Параметры запроса"/>:<CopyValue value={query.params} label="параметры запроса"/>}<p>Полный ответ</p><CopyValue value={result} label="полный ответ запроса"/></details></>
}

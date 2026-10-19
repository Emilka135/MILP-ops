import React,{useEffect,useMemo,useState} from 'react';
import './FeedbackPanel.css';

const STATES={proposed:'Подготовлено ночью',approved:'Одобрено для проверки',suspended:'Приостановлено после жалобы',rejected:'Отклонено',active:'Нужна проверка развёртывания'};
const THEMES={повтор:'Слышимость и повторные вопросы',темп:'Скорость речи',навигация:'Меню и переход к человеку',слоты:'Подбор времени',распознавание:'Распознавание слов',предложения:'Другие предложения'};
function structured(value,fallback){if(typeof value!=='string')return value??fallback;try{return JSON.parse(value)}catch{return fallback}}
function stamp(value){const date=new Date(value);return Number.isNaN(date.getTime())?'Время не указано':date.toLocaleString('ru-RU',{timeZone:'Europe/Moscow',dateStyle:'short',timeStyle:'short'})}
function summaryError(error){const text=error?.message||'Не удалось выполнить действие. Повторите запрос.';return text==='unresolved_complaints'?'Сначала разберите открытые жалобы на этом номере.':text}

export default function FeedbackPanel({api,identity}){
 const [data,setData]=useState(null),[query,setQuery]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(''),[notes,setNotes]=useState({});
 useEffect(()=>{if(identity?.role!=='admin')return;let cancelled=false;api('insights/dashboard').then(value=>{if(!cancelled){setData(value);setError('')}}).catch(err=>{if(!cancelled)setError(summaryError(err))});return()=>{cancelled=true}},[api,identity?.role]);
 async function load(){setBusy('refresh');try{setData(await api('insights/dashboard'));setError('')}catch(err){setError(summaryError(err))}finally{setBusy('')}}
 async function act(key,path,payload,message){setBusy(key);setError('');setNotice('');try{const result=await api(path,payload);if(result?.error||result?.status==='blocked')throw Error(result.error||'Не удалось выполнить действие');setData(await api('insights/dashboard'));setNotice(message)}catch(err){setError(summaryError(err))}finally{setBusy('')}}
 const feedback=data?.feedback||[],complaints=data?.complaints||[],profiles=data?.profiles||[],groups=data?.clusters||[],metrics=data?.metrics||[];
 const findFeedback=id=>feedback.find(row=>row.id===id);
 const filter=row=>!query.trim()||[row.registrar,row.text,row.call_id,row.label].some(value=>String(value||'').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
 const pending=(data?.jobs||[]).filter(job=>job.status==='queued').length;
 const allReviews=metrics.reduce((sum,row)=>sum+row.feedback_count,0);
 const openComplaints=complaints.filter(row=>row.state==='open');
 const visibleFeedback=useMemo(()=>feedback.filter(filter),[data,query]);
 if(identity?.role!=='admin')return null;
 return <div className="feedback-panel">
  <p className="feedback-intro">После звонка спрашиваем: «Что исправить в тестовом варианте?» Здесь видны ответы, жалобы и предложения для отдельных рабочих номеров.</p>
  <div className="feedback-tools"><label><span>Найти номер, звонок или слова пациента</span><input aria-label="Поиск отзывов" placeholder="Например, медленнее" value={query} onChange={event=>setQuery(event.target.value)}/></label><button className="secondary" disabled={!!busy} onClick={load}>Обновить</button></div>
  {error&&<p className="feedback-error" role="alert">{error}</p>}{notice&&<p className="feedback-notice" role="status">{notice}</p>}
  {!data&&!error&&<p role="status">Загружаем отзывы…</p>}
  {data&&<>
   <div className="feedback-overview"><span><strong>{allReviews}</strong> отзывов</span><span><strong>{openComplaints.length}</strong> открытых жалоб</span><span><strong>{pending}</strong> задач ждут закрытия регистратуры</span></div>
   <section className="feedback-section"><h3>1. Что чаще предлагают исправить</h3><p className="feedback-muted">Группируем ответы после закрытия регистратуры по её общему расписанию. Это местная группировка по словам, её выводы проверяет человек. Время показано по Москве.</p>
    {!groups.length&&<p className="feedback-empty">Ночная сводка пока не готова. Новые ответы уже сохраняются.</p>}
    {groups.filter(group=>!query.trim()||String(group.registrar).includes(query.trim())||structured(group.terms,[]).some(term=>term.toLocaleLowerCase().includes(query.toLocaleLowerCase()))).map(group=><article className="feedback-card" key={group.id}><div className="feedback-card-heading"><strong>{THEMES[group.title]||group.title}</strong><span>{group.size} отзывов · номер {group.registrar||'не указан'}</span></div><p className="feedback-terms">{structured(group.terms,[]).join(' · ')}</p><small>Обработано {stamp(group.updated_at)}</small></article>)}
   </section>
   <section className="feedback-section"><h3>2. Жалобы, которые нужно разобрать</h3><p className="feedback-muted">Открытая жалоба приостанавливает персональные изменения только на её рабочем номере. Закрытие жалобы само их не включает.</p>
    {!complaints.length&&<p className="feedback-empty">Жалоб пока нет.</p>}
    {complaints.filter(row=>filter({...findFeedback(row.feedback_id),...row})).map(row=><article className={'feedback-card '+(row.state==='open'?'feedback-complaint':'')} key={row.id}><div className="feedback-card-heading"><strong>Номер {row.registrar}</strong><span>{row.state==='open'?'Нужен разбор':'Разобрано'}</span></div><blockquote>{row.text}</blockquote><small>Звонок {findFeedback(row.feedback_id)?.call_id||'не указан'}</small>{row.state==='open'?<form className="feedback-resolution" onSubmit={event=>{event.preventDefault();act('complaint-'+row.id,'insights/complaint/resolve',{id:row.id,resolution:(notes[row.id]||'').trim()},'Решение сохранено. Персональные изменения остаются приостановлены.')}}><label><span>Что проверили и исправили</span><textarea aria-label={'Решение по жалобе '+row.id} maxLength={4000} value={notes[row.id]||''} onChange={event=>setNotes({...notes,[row.id]:event.target.value})} required rows={3}/></label><button className="primary" disabled={!!busy||!(notes[row.id]||'').trim()}>Сохранить решение</button></form>:<p className="feedback-resolution-note">Решение: {row.resolution}</p>}</article>)}
   </section>
   <section className="feedback-section"><h3>3. Предложения для рабочих номеров</h3><p className="feedback-muted">Одобрение сохраняет решение администратора. Предложение остаётся черновиком до отдельной проверки и развёртывания; автоматически разговоры не меняются.</p>
    {!profiles.length&&<p className="feedback-empty">Подготовленных предложений пока нет.</p>}
    {profiles.filter(filter).map(profile=>{const proposal=structured(profile.proposal,{}),blocked=openComplaints.some(row=>row.registrar===profile.registrar);return <article className="feedback-card" key={profile.id}><div className="feedback-card-heading"><strong>{profile.label}</strong><span>Номер {profile.registrar}</span></div><p>{STATES[profile.state]||'Требует проверки'} · {stamp(profile.created_at)}</p><p className="feedback-muted">Использовано отзывов этого номера: {proposal.evidence_count||0}. {Object.keys(proposal.themes||{}).map(theme=>THEMES[theme]||theme).join(', ')||'Данных для выделения темы пока мало.'}</p>{blocked&&<p className="feedback-blocked">Сначала разберите открытые жалобы этого номера.</p>}<div className="feedback-actions">{['proposed','suspended'].includes(profile.state)&&<button className="primary" disabled={!!busy||blocked} onClick={()=>act('profile-'+profile.id,'insights/profile/review',{id:profile.id,approve:true},'Предложение одобрено для проверки. Оно остаётся черновиком и не включено в звонках.')}>Одобрить предложение</button>}{profile.state!=='rejected'&&<button className="secondary" disabled={!!busy} onClick={()=>act('profile-'+profile.id,'insights/profile/review',{id:profile.id,approve:false},'Предложение отклонено.')}>Отклонить</button>}</div></article>})}
   </section>
   <section className="feedback-section"><h3>4. Ответы пациентов</h3><p className="feedback-muted">{data.rating_note||'Текст не превращается в оценку по шкале. Средняя оценка учитывает только явно названные пациентами числа.'}</p>
    {metrics.map(row=><p className="feedback-metric" key={row.registrar}>Номер {row.registrar}: {row.feedback_count} отзывов, жалоб {row.complaint_count} ({Math.round(row.complaint_share*100)}%). Явно названных оценок: {row.explicit_rating_count}{row.explicit_rating_count>0&&` · средняя ${Number(row.explicit_rating_mean).toFixed(1)}`}.</p>)}
    {!visibleFeedback.length&&<p className="feedback-empty">{query?'По этому запросу ответов нет.':'Ответы появятся после первых звонков.'}</p>}
    {visibleFeedback.map(row=><article className="feedback-card" key={row.id}><div className="feedback-card-heading"><strong>Звонок {row.call_id}</strong><span>Номер {row.registrar}</span></div><blockquote>{row.text}</blockquote><small>{stamp(row.created_at)} · {row.kind==='complaint'?'Жалоба':row.kind==='praise'?'Положительный отзыв':'Предложение'} · {row.cluster_id?'Обработано ночью':'Ждёт ночной обработки'}</small>{row.rating_source==='patient_explicit'&&<p className="feedback-muted">Пациент сам назвал оценку: {row.rating} из 5.</p>}</article>)}
   </section>
  </>}
 </div>
}

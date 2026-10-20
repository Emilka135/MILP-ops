import React from 'react';
export default function CallQueries({queries=[]}){
 return <details className="call-queries"><summary>Запросы к тестовой таблице · {queries.length}</summary>{queries.length?queries.map(q=><article key={q.id}><p>Шаг {(q.step_index??0)+1} · {q.state} · {q.created_at}</p><pre>{q.sql}</pre><p>Параметры</p><pre>{JSON.stringify(q.params,null,2)}</pre><p>Ответ таблицы</p><pre>{JSON.stringify(q.result,null,2)}</pre></article>):<p>На этом шаге запросов не выполнялось.</p>}</details>
}

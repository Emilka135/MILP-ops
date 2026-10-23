import React from 'react';
import {QueryAnswer} from './QueryData';
export default function CallQueries({queries=[]}){
 return <details className="call-queries"><summary>Сохранённые запросы и ответы · {queries.length}</summary>{queries.length?queries.map(q=><article key={q.id}><p>Шаг {(q.step_index??0)+1} · {q.state} · {q.created_at}</p><QueryAnswer query={q}/></article>):<p>На этом шаге запросов не выполнялось.</p>}</details>
}

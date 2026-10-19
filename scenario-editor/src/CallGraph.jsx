import React,{useEffect,useMemo,useRef,useState} from 'react';
import {ReactFlow,Background,Controls} from '@xyflow/react';
export default function CallGraph({call}){
 const flow=useRef(null);
 const [selected,setSelected]=useState(0),[expanded,setExpanded]=useState({});
 const graph=useMemo(()=>{
  const titles=call.graph?.titles||{},branches=call.graph?.branches||{},nodes=[],edges=[];
  const trace=call.trace||[];
  trace.forEach((ref,i)=>{
   const state=ref.replace(/^demo\./,'');
   nodes.push({id:'step'+i,position:{x:0,y:i*130},data:{label: `${i+1}. ${titles[state]||state}`},style:{width:260,background:i===selected?'#d9f1ee':'#fff',border:'2px solid #28786f',cursor:'pointer'}});
   if(i)edges.push({id:'path'+i,source:'step'+(i-1),target:'step'+i,animated:false,label:'Разговор',style:{stroke:'#28786f',strokeWidth:3}});
   if(expanded[i]) (branches[state]||[]).filter(b=>'demo.'+b!==trace[i+1]).forEach((b,j)=>{
    const id=`branch${i}-${j}`;nodes.push({id,position:{x:330,y:i*130+j*75+40},data:{label:titles[b]||b},style:{width:240,background:'#f1f3f6',border:'1px dashed #8c959e'}});
    edges.push({id:'edge'+id,source:'step'+i,target:id,label:'Другая ветка',style:{stroke:'#929ca5',strokeDasharray:'5 4'}});
   });
  });return {nodes,edges};
 },[call,selected,expanded]);
 useEffect(()=>{const t=setTimeout(()=>{const nearby=graph.nodes.filter(n=>n.id.startsWith('step')?Math.abs(Number(n.id.slice(4))-selected)<=1:n.id.startsWith('branch'+selected+'-'));flow.current?.fitView({nodes:nearby,padding:.2,minZoom:.6,maxZoom:1,duration:100})},180);return()=>clearTimeout(t)},[graph.nodes.length,selected]);
 const turns=call.transcript?.filter(t=>t.step_index===selected || (t.step_index===undefined&&t.state===call.trace?.[selected]))||[];
 return <div className="call-graph-view"><p>Основная линия — путь этого разговора. Нажмите шаг, чтобы раскрыть другие ветки и прочитать реплики.</p><div className="call-step-picker" aria-label="Шаги разговора">{call.trace?.map((ref,i)=><button key={i} aria-pressed={selected===i} onClick={()=>setSelected(i)}>{i+1}. {call.graph?.titles?.[ref.replace(/^demo\./,'')]||ref}</button>)}</div><div className="call-graph"><ReactFlow onInit={instance=>{flow.current=instance}} nodes={graph.nodes} edges={graph.edges} nodesDraggable={false} nodesConnectable={false} fitView minZoom={.15} maxZoom={1.5} onNodeClick={(_,node)=>{if(node.id.startsWith('step')){const i=Number(node.id.slice(4));setSelected(i);setExpanded(v=>({...v,[i]:!v[i]}))}}}><Background/><Controls showInteractive={false}/></ReactFlow></div><div className="graph-turns" aria-live="polite"><h3>Шаг {selected+1}: {call.graph?.titles?.[call.trace?.[selected]?.replace(/^demo\./,'')]}</h3>{turns.map((t,i)=><p key={i}><strong>{t.role==='assistant'?(call.analysis?'Сотрудник':'Помощник'):t.role==='dtmf'?'Клавиша':'Пациент'}:</strong> {t.text} <small>{({typed_test:'введено текстом',real_asr:'распознано с микрофона',dtmf:'клавиша',imported_transcript:'из загруженной расшифровки'})[t.source]||''}</small></p>)}{!turns.length&&<p>У этого шага нет записанной реплики.</p>}</div></div>
}

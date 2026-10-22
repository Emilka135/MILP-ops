import React,{useEffect,useRef,useState} from 'react';
import {Database,GripVertical,Phone,RotateCcw} from 'lucide-react';

const KEY='clinic-workspace-layout-v1';
const SIDES={left:'Слева',right:'Справа',top:'Сверху',bottom:'Снизу',float:'Отдельное окно'};
const DEFAULTS={calls:{side:'left',width:270,height:230,x:24,y:30},mis:{side:'right',width:400,height:330,x:420,y:70}};
const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));
function readLayout(){
 try{const raw=JSON.parse(localStorage.getItem(KEY)),result={};for(const id of ['calls','mis']){const item=raw?.[id]||{};result[id]={side:Object.hasOwn(SIDES,item.side)?item.side:DEFAULTS[id].side};for(const key of ['width','height','x','y'])result[id][key]=Number.isFinite(item[key])?clamp(item[key],key==='width'?225:key==='height'?160:0,1600):DEFAULTS[id][key]}return result}catch{return structuredClone(DEFAULTS)}
}
function rectangles(layout,{width,height}){
 const gap=8,groups=Object.fromEntries(['left','right','top','bottom'].map(side=>[side,Object.keys(layout).filter(id=>layout[id].side===side)]));
 let left=groups.left.length?Math.max(...groups.left.map(id=>layout[id].width)):0,right=groups.right.length?Math.max(...groups.right.map(id=>layout[id].width)):0;
 const scale=Math.min(1,Math.max(0,width-320-2*gap)/(left+right||1));left*=scale;right*=scale;
 const x=left?left+gap:0,centerWidth=Math.max(0,width-x-(right?right+gap:0));
 let top=groups.top.length?Math.max(...groups.top.map(id=>layout[id].height)):0,bottom=groups.bottom.length?Math.max(...groups.bottom.map(id=>layout[id].height)):0;
 const vscale=Math.min(1,Math.max(0,height-240-2*gap)/(top+bottom||1));top*=vscale;bottom*=vscale;
 const y=top?top+gap:0,result={center:{x,y,width:centerWidth,height:Math.max(0,height-y-(bottom?bottom+gap:0))}};
 for(const side of ['left','right'])groups[side].forEach((id,i)=>{const h=(height-gap*(groups[side].length-1))/groups[side].length;result[id]={x:side==='left'?0:width-right,y:i*(h+gap),width:side==='left'?left:right,height:h}});
 for(const side of ['top','bottom'])groups[side].forEach((id,i)=>{const w=(centerWidth-gap*(groups[side].length-1))/groups[side].length;result[id]={x:x+i*(w+gap),y:side==='top'?0:height-bottom,width:w,height:side==='top'?top:bottom}});
 for(const id of Object.keys(layout))if(layout[id].side==='float'){const item=layout[id],w=Math.min(item.width,width),h=Math.min(item.height,height);result[id]={x:clamp(item.x,0,width-w),y:clamp(item.y,0,height-h),width:w,height:h}}
 return result;
}
const position=rect=>({left:rect.x,top:rect.y,width:rect.width,height:rect.height});

export default function DockWorkspace({calls,mis,children}){
 const host=useRef(null),gesture=useRef(null),[layout,setLayout]=useState(readLayout),[size,setSize]=useState({width:1200,height:800}),[drag,setDrag]=useState(null),[notice,setNotice]=useState('');
 const rects=rectangles(layout,size);
 useEffect(()=>{const escape=e=>{if(e.key==='Escape'&&gesture.current){setLayout(gesture.current.layout);gesture.current=null;setDrag(null)}};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape)},[]);
 useEffect(()=>{const el=host.current,observer=new ResizeObserver(([entry])=>{const {width,height}=entry.contentRect;setSize(old=>old.width===width&&old.height===height?old:{width,height})});observer.observe(el);return()=>observer.disconnect()},[]);
 function store(next){setLayout(next);try{localStorage.setItem(KEY,JSON.stringify(next));setNotice('Расположение сохранено')}catch{setNotice('Расположение изменено. Браузер не разрешил сохранить его после закрытия вкладки.')}}
 function place(id,side){store({...layout,[id]:{...layout[id],side}})}
 function reset(){gesture.current=null;setDrag(null);store(structuredClone(DEFAULTS))}
 function down(e,id,kind){if(e.button!==0||window.innerWidth<=760)return;e.preventDefault();e.currentTarget.setPointerCapture(e.pointerId);const box=host.current.getBoundingClientRect();gesture.current={id,kind,startX:e.clientX,startY:e.clientY,rect:rects[id],layout,box};if(kind==='move')setDrag({id,x:e.clientX-box.left,y:e.clientY-box.top,side:null})}
 function move(e){const g=gesture.current;if(!g)return;const dx=e.clientX-g.startX,dy=e.clientY-g.startY;
  if(g.kind==='move'){const x=e.clientX-g.box.left,y=e.clientY-g.box.top,zone=70;let side=null;if(x<zone)side='left';else if(x>size.width-zone)side='right';else if(y<zone)side='top';else if(y>size.height-zone)side='bottom';setDrag({id:g.id,x,y,side});return}
  const item=g.layout[g.id],next={...g.layout},vertical=['top','bottom'].includes(item.side);const width=clamp(g.rect.width+dx*(item.side==='right'?-1:1),225,Math.max(225,size.width-320)),height=clamp(g.rect.height+dy*(item.side==='bottom'?-1:1),160,Math.max(160,size.height-240));
  for(const id of Object.keys(next))if(id===g.id||next[id].side===item.side&&item.side!=='float')next[id]={...next[id],...(vertical?{height}:item.side==='float'?{width,height}:{width})};g.latest=next;setLayout(next)
 }
 function up(e){const g=gesture.current;if(!g)return;if(g.kind==='move'){const x=e.clientX-g.box.left,y=e.clientY-g.box.top,zone=70;const side=x<zone?'left':x>size.width-zone?'right':y<zone?'top':y>size.height-zone?'bottom':'float';store({...g.layout,[g.id]:{...g.layout[g.id],side,x:clamp(x-g.rect.width/2,0,Math.max(0,size.width-g.rect.width)),y:clamp(y-20,0,Math.max(0,size.height-g.rect.height))}})}else store(g.latest||g.layout);gesture.current=null;setDrag(null);if(e.currentTarget.hasPointerCapture(e.pointerId))e.currentTarget.releasePointerCapture(e.pointerId)}
 function cancel(){if(gesture.current){setLayout(gesture.current.layout);gesture.current=null}setDrag(null)}
 function resizeKey(e,id){const item=layout[id],vertical=['top','bottom'].includes(item.side),key=vertical?'height':'width',min=vertical?160:225,max=Math.max(min,vertical?size.height-240:size.width-320);let value=item[key];if(e.key==='Home')value=min;else if(e.key==='End')value=max;else if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))value+=['ArrowLeft','ArrowUp'].includes(e.key)?-24:24;else return;e.preventDefault();const next={...layout};for(const other of Object.keys(next))if(other===id||next[other].side===item.side&&item.side!=='float')next[other]={...next[other],[key]:clamp(value,min,max)};store(next)}
 return <><div className="dock-toolbar"><span>Панели: тяните заголовок к краю или выберите расположение</span><button type="button" onClick={reset}><RotateCcw size={13}/>Вернуть исходный вид</button></div><div className="dock-workspace workshop-layout" ref={host} data-dragging={!!drag}>
  {['calls','mis'].map(id=>{const title=id==='calls'?'Звонки':'МИС',Icon=id==='calls'?Phone:Database,vertical=['top','bottom'].includes(layout[id].side);return <section key={id} data-panel={id} data-placement={layout[id].side} className={'dock-panel '+(id==='calls'?'quest-sidebar':'query-sidebar')+(layout[id].side==='float'?' dock-floating':'')} style={position(rects[id])} aria-label={id==='calls'?'Панель звонков':'Панель ответов выбранного звонка'}>
   <header className="dock-panel-header"><button type="button" className="dock-grip" aria-label={'Переместить панель '+title} title="Потяните заголовок к краю. Стрелки на клавиатуре закрепляют панель." onPointerDown={e=>down(e,id,'move')} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel} onKeyDown={e=>{if(e.key==='Escape'){cancel();return}const side={ArrowLeft:'left',ArrowRight:'right',ArrowUp:'top',ArrowDown:'bottom'}[e.key];if(side){e.preventDefault();place(id,side)}}}><GripVertical size={13}/><Icon size={14}/><h2>{title}</h2></button><select aria-label={'Расположение панели '+title} value={layout[id].side} onChange={e=>place(id,e.target.value)}>{Object.entries(SIDES).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></header>
   <div className="dock-panel-body">{id==='calls'?calls:mis}</div><div className="dock-resizer workshop-splitter" role="separator" tabIndex={0} aria-label={id==='mis'?'Ширина панели базы данных':'Размер панели звонков'} aria-orientation={vertical?'horizontal':'vertical'} aria-valuemin={vertical?160:225} aria-valuemax={Math.max(vertical?160:225,vertical?size.height-240:size.width-320)} aria-valuenow={Math.round(vertical?rects[id].height:rects[id].width)} title="Изменить размер: потяните мышью или нажимайте стрелки. Home — минимум, End — максимум." onPointerDown={e=>down(e,id,'resize')} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel} onKeyDown={e=>resizeKey(e,id)} onDoubleClick={()=>store({...layout,[id]:{...layout[id],width:DEFAULTS[id].width,height:DEFAULTS[id].height}})}/>
  </section>})}
  <main className="quest-main dock-center" style={position(rects.center)} aria-label="Разговор и маршрут">{children}</main>
  {drag&&<><div className="dock-ghost" style={{left:clamp(drag.x-100,0,size.width-200),top:clamp(drag.y+14,0,size.height-50)}}>{drag.id==='calls'?'Звонки':'МИС'} · {drag.side?SIDES[drag.side]:'Отдельное окно'}</div>{['left','right','top','bottom'].map(side=><div key={side} className={'dock-target '+side+(drag.side===side?' drop-ready':'')}>{SIDES[side]}</div>)}</>}
 </div><div className="dock-announcement" role="status" aria-live="polite">{notice}</div></>
}

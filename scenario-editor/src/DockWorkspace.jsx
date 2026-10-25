import React,{useEffect,useMemo,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import {GripVertical,PanelsTopLeft,RotateCcw,ExternalLink,X} from 'lucide-react';
import {LAYOUT_KEY,leaf,leaves,panelGroup,placePanel,preset,rectangles,removePanel,updateNode,validateLayout} from './dockTree';
import './RecursiveDock.css';

const EDGES={center:'Во вкладку',left:'Слева',right:'Справа',top:'Сверху',bottom:'Снизу'};
const FLOAT_KEY='clinic-workspace-floating-v2';
const clamp=(n,min,max)=>Math.max(min,Math.min(max,n));
const position=rect=>({left:rect.x,top:rect.y,width:Math.max(0,rect.width),height:Math.max(0,rect.height)});
function load(ids){try{return validateLayout(JSON.parse(localStorage.getItem(LAYOUT_KEY)),ids)||preset('default',ids)}catch{return preset('default',ids)}}
function loadWorkspace(ids){
 let floating=[];
 try{const saved=JSON.parse(localStorage.getItem(FLOAT_KEY));if(Array.isArray(saved)){const seen=new Set();floating=saved.filter(item=>item&&ids.includes(item.id)&&!seen.has(item.id)&&['x','y','width','height'].every(key=>Number.isFinite(item[key]))&&seen.add(item.id)).map(item=>({id:item.id,x:clamp(item.x,0,4000),y:clamp(item.y,0,4000),width:clamp(item.width,240,2400),height:clamp(item.height,160,2400)}))}}catch{}
 let tree=load(ids);for(const item of floating)tree=removePanel(tree,item.id);
 // A floating-only workspace intentionally has no docked tree.
 try{if(localStorage.getItem(LAYOUT_KEY)==='null')tree=null}catch{}
 return {tree,floating};
}
function hit(groups,x,y){
 const rect=groups.find(g=>x>=g.x&&x<=g.x+g.width&&y>=g.y&&y<=g.y+g.height);
 if(!rect)return null;
 const dx=(x-rect.x)/Math.max(1,rect.width),dy=(y-rect.y)/Math.max(1,rect.height);
 const distances={left:dx,right:1-dx,top:dy,bottom:1-dy};
 const edge=Object.entries(distances).sort((a,b)=>a[1]-b[1])[0];
 return {rect,edge:edge[1]<.25?edge[0]:'center'};
}

export default function DockWorkspace({panels,calls,mis,children,activePanel,onActivePanel}){
 const [toolbarTarget,setToolbarTarget]=useState(null);
 useEffect(()=>{setToolbarTarget(document.getElementById('workspace-panel-controls'))},[]);
 const registry=panels?.length?panels:[{id:'calls',title:'Звонки',content:calls},{id:'conversation',title:'Разговор и маршрут',content:children},{id:'mis',title:'МИС',content:mis}];
 const ids=registry.map(p=>p.id),signature=ids.join('|'),host=useRef(null),gesture=useRef(null),lastActivePanel=useRef(activePanel),initial=useRef(null);
 if(!initial.current)initial.current=loadWorkspace(ids);
 const [tree,setTree]=useState(initial.current.tree),[floating,setFloating]=useState(initial.current.floating),[size,setSize]=useState({width:1200,height:800}),[drag,setDrag]=useState(null),[notice,setNotice]=useState(''),[selected,setSelected]=useState(''),[target,setTarget]=useState(''),[edge,setEdge]=useState('right');
 const layout=useMemo(()=>rectangles(tree,size.width,size.height),[tree,size]);
 const getTitle=id=>registry.find(p=>p.id===id)?.title||id;
 const visible=leaves(tree),selectedId=ids.includes(selected)?selected:ids[0],targetId=ids.includes(target)?target:visible[0]?.active||ids[0];
 useEffect(()=>{setTree(old=>!old?null:validateLayout(old,ids)||load(ids))},[signature]);
 useEffect(()=>{const el=host.current;if(!el)return;const observer=new ResizeObserver(([entry])=>{const {width,height}=entry.contentRect;setSize(old=>old.width===width&&old.height===height?old:{width,height})});observer.observe(el);return()=>observer.disconnect()},[]);
 function store(next,text='Расположение сохранено',nextFloating=floating){
  setTree(next);setFloating(nextFloating);try{localStorage.setItem(LAYOUT_KEY,JSON.stringify(next));localStorage.setItem(FLOAT_KEY,JSON.stringify(nextFloating));setNotice(text)}catch{setNotice('Расположение изменено, но браузер не разрешил сохранить настройки.')}
 }
 function cancel(){const g=gesture.current;if(g?.kind==='resize')setTree(g.original);if(g?.originalFloating)setFloating(g.originalFloating);gesture.current=null;setDrag(null)}
 useEffect(()=>{const escape=e=>{if(e.key==='Escape')cancel()};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape)},[]);
 function activate(group,id){onActivePanel?.(id);setSelected(id);if(group.floating){store(tree,'Открыта панель '+getTitle(id),[...floating.filter(item=>item.id!==id),floating.find(item=>item.id===id)]);return}store(updateNode(tree,group.id,node=>({...node,active:id})),'Открыта панель '+getTitle(id))}
 function show(id){onActivePanel?.(id);const group=panelGroup(tree,id),float=floating.find(item=>item.id===id);if(float)activate({floating:true},id);else if(group)activate(group,id);else if(visible.length)store(placePanel(tree,id,visible[0].id,'center'));else store(leaf([id]));setSelected(id)}
 // Navigation opens the requested panel when the requested id changes. Initial
 // mount intentionally preserves the user's saved SQL/API-only workspace.
 useEffect(()=>{if(lastActivePanel.current===activePanel)return;lastActivePanel.current=activePanel;if(ids.includes(activePanel))show(activePanel)},[activePanel]);
 function floatPanel(id,point){const existing=floating.find(item=>item.id===id);if(existing){show(id);return}const rect=layout.groups.find(item=>item.group.tabs.includes(id));const item={id,x:point?.x??Math.min(50+floating.length*24,size.width/5),y:point?.y??Math.min(35+floating.length*24,size.height/5),width:Math.min(size.width,Math.max(300,Math.min(rect?.width||500,650))),height:Math.min(size.height,Math.max(200,Math.min(rect?.height||400,500)))};store(removePanel(tree,id),'Панель открыта отдельным окном',[...floating,item]);setSelected(id)}
 function place(id,to,where){if(where==='float'){floatPanel(id);return}const group=panelGroup(tree,to),remaining=floating.filter(item=>item.id!==id);if(!group){store(tree?placePanel(tree,id,visible[0].id,where):leaf([id]),'Панель закреплена',remaining)}else store(placePanel(tree,id,group.id,where),'Расположение сохранено',remaining);setSelected(id)}
 function dock(id){if(visible.length)place(id,visible[0].active,'center');else store(leaf([id]),'Панель закреплена',floating.filter(item=>item.id!==id))}
 function closePanel(id){store(removePanel(tree,id),'Панель скрыта',floating.filter(item=>item.id!==id))}
 function raiseFloat(id){if(floating.at(-1)?.id===id)return;const item=floating.find(item=>item.id===id);if(item)store(tree,'Панель выбрана',[...floating.filter(item=>item.id!==id),item])}
 function floatRect(item,index){const width=Math.min(item.width,size.width),height=Math.min(item.height,size.height);return {x:clamp(item.x,0,Math.max(0,size.width-width)),y:clamp(item.y,0,Math.max(0,size.height-height)),width,height,z:40+index*3,group:{id:'floating-'+item.id,type:'tabs',tabs:[item.id],active:item.id,floating:true}}}
 const floatingRects=floating.map(floatRect),allRects=[...layout.groups,...floatingRects];
 function startMove(e,id){
  if(e.button!==0||e.target.closest('[data-close-panel]'))return;
  e.preventDefault();e.currentTarget.setPointerCapture(e.pointerId);
  const rect=floatingRects.find(item=>item.group.active===id);
  gesture.current={kind:rect?'float-move':'move',id,rect,startX:e.clientX,startY:e.clientY,original:tree,originalFloating:floating};
 }
 function startResize(e,divider){if(e.button!==0)return;e.preventDefault();e.currentTarget.setPointerCapture(e.pointerId);gesture.current={kind:'resize',divider,original:tree}}
 function startFloatResize(e,rect){if(e.button!==0)return;e.preventDefault();e.currentTarget.setPointerCapture(e.pointerId);gesture.current={kind:'float-resize',id:rect.group.active,rect,startX:e.clientX,startY:e.clientY,original:tree,originalFloating:floating}}
 function move(e){
  const g=gesture.current;if(!g)return;
  const box=host.current.getBoundingClientRect(),x=e.clientX-box.left,y=e.clientY-box.top;
  if(g.kind==='float-move'||g.kind==='float-resize'){
   const dx=e.clientX-g.startX,dy=e.clientY-g.startY,rect=g.rect,changes=g.kind==='float-move'?{x:clamp(rect.x+dx,0,Math.max(0,size.width-rect.width)),y:clamp(rect.y+dy,0,Math.max(0,size.height-rect.height))}:{width:clamp(rect.width+dx,Math.min(260,size.width),Math.max(0,size.width-rect.x)),height:clamp(rect.height+dy,Math.min(180,size.height),Math.max(0,size.height-rect.y))};
   g.latestFloating=g.originalFloating.map(item=>item.id===g.id?{...item,...changes}:item);setFloating(g.latestFloating);return;
  }
  if(g.kind==='move'){
   if(Math.hypot(e.clientX-g.startX,e.clientY-g.startY)<5&&!drag)return;
   setDrag({id:g.id,x,y,target:hit(layout.groups,x,y)});return;
  }
  const d=g.divider,horizontal=d.node.axis==='row',length=horizontal?d.parent.width:d.parent.height;
  const ratio=clamp(((horizontal?x:y)-(horizontal?d.parent.x:d.parent.y))/Math.max(1,length-6),.12,.88);
  const next=updateNode(g.original,d.node.id,node=>({...node,ratio}));g.latest=next;setTree(next);
 }
 function up(e){
  const g=gesture.current;if(!g)return;
  if(g.kind==='move'){
   if(Math.hypot(e.clientX-g.startX,e.clientY-g.startY)>=5){const box=host.current.getBoundingClientRect(),drop=hit(layout.groups,e.clientX-box.left,e.clientY-box.top);if(drop)store(placePanel(tree,g.id,drop.rect.group.id,drop.edge));else floatPanel(g.id,{x:e.clientX-box.left-120,y:e.clientY-box.top-15})}
   else {const group=panelGroup(tree,g.id);if(group)activate(group,g.id)}
  }else if(g.kind==='float-move'||g.kind==='float-resize'){const next=g.latestFloating||g.originalFloating;store(tree,'Размер и положение окна сохранены',[...next.filter(item=>item.id!==g.id),next.find(item=>item.id===g.id)])}
  else store(g.latest||g.original);
  gesture.current=null;setDrag(null);if(e.currentTarget.hasPointerCapture(e.pointerId))e.currentTarget.releasePointerCapture(e.pointerId);
 }
 function resizeKey(e,divider){
  const keys=divider.node.axis==='row'?['ArrowLeft','ArrowRight']:['ArrowUp','ArrowDown'];let ratio=divider.node.ratio;
  if(e.key==='Home')ratio=.12;else if(e.key==='End')ratio=.88;else if(keys.includes(e.key))ratio+=(e.key===keys[0]?-1:1)*(e.shiftKey?.1:.025);else return;
  e.preventDefault();store(updateNode(tree,divider.node.id,node=>({...node,ratio:clamp(ratio,.12,.88)})));
 }
 function floatResizeKey(e,rect){let {width,height}=rect;if(e.key==='Home'){width=280;height=180}else if(e.key==='End'){width=size.width-rect.x;height=size.height-rect.y}else if(e.key==='ArrowLeft')width-=24;else if(e.key==='ArrowRight')width+=24;else if(e.key==='ArrowUp')height-=24;else if(e.key==='ArrowDown')height+=24;else return;e.preventDefault();store(tree,'Размер окна сохранён',floating.map(item=>item.id===rect.group.active?{...item,width:clamp(width,Math.min(260,size.width),size.width-rect.x),height:clamp(height,Math.min(180,size.height),size.height-rect.y)}:item))}
 function tabKey(e,group,id){
  if(e.altKey&&['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){
   e.preventDefault();const other=visible.find(g=>g.id!==group.id);if(other)place(id,other.active,{ArrowLeft:'left',ArrowRight:'right',ArrowUp:'top',ArrowDown:'bottom'}[e.key]);return;
  }
  if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){
   e.preventDefault();let index=group.tabs.indexOf(id);index=e.key==='Home'?0:e.key==='End'?group.tabs.length-1:(index+(e.key==='ArrowLeft'?-1:1)+group.tabs.length)%group.tabs.length;
   activate(group,group.tabs[index]);requestAnimationFrame(()=>host.current?.querySelector(`[data-tab-panel="${group.tabs[index]}"]`)?.focus());
  }
 }
 const dropPreview=drag?.target&&(()=>{const {rect,edge}=drag.target,r={...rect};if(edge==='left')r.width/=2;if(edge==='right'){r.x+=r.width/2;r.width/=2}if(edge==='top')r.height/=2;if(edge==='bottom'){r.y+=r.height/2;r.height/=2}return r})();
 const toolbar=<div className="recursive-toolbar dock-toolbar">
   <span><PanelsTopLeft size={14}/>Рабочее пространство</span>
   <label>Вид<select aria-label="Готовое расположение панелей" value="" onChange={e=>{cancel();const next=preset(e.target.value,ids);store(next,'Расположение сохранено',[]);onActivePanel?.(leaves(next)[0]?.active);e.target.value=''}}><option value="" disabled>Выбрать…</option><option value="default">Звонок и МИС</option><option value="split">Два окна слева, одно справа</option>{ids.includes('api')&&<option value="api">Только ручные API</option>}{ids.includes('sql')&&<option value="sql">Только SQL</option>}</select></label>
   <details className="recursive-panel-menu"><summary>Панели</summary><div>{registry.map(panel=><button key={panel.id} type="button" aria-pressed={!!panelGroup(tree,panel.id)||floating.some(item=>item.id===panel.id)} onClick={()=>show(panel.id)}>{panelGroup(tree,panel.id)||floating.some(item=>item.id===panel.id)?'✓ ':''}{panel.title}</button>)}</div></details>
   <details className="recursive-place-menu"><summary>Разместить панель</summary><div><label>Панель<select aria-label="Перемещаемая панель" value={selectedId} onChange={e=>setSelected(e.target.value)}>{registry.map(p=><option key={p.id} value={p.id}>{p.title}</option>)}</select></label><label>Относительно<select aria-label="Целевая панель" value={targetId} onChange={e=>setTarget(e.target.value)}>{visible.flatMap(g=>g.tabs).map(id=><option key={id} value={id}>{getTitle(id)}</option>)}</select></label><label>Расположение<select aria-label="Способ размещения" value={edge} onChange={e=>setEdge(e.target.value)}>{Object.entries({...EDGES,float:'Отдельное окно'}).map(([key,title])=><option key={key} value={key}>{title}</option>)}</select></label><button type="button" onClick={()=>place(selectedId,targetId,edge)}>Разместить</button></div></details>
   <span className="recursive-help">Тяните вкладку к краю любой области; в центр — объединить</span>
   <button type="button" className="recursive-reset" onClick={()=>{cancel();store(preset('default',ids),'Исходный вид восстановлен',[])}}><RotateCcw size={13}/>Исходный вид</button>
  </div>;
 return <>
  {toolbarTarget?createPortal(toolbar,toolbarTarget):toolbar}
  <div className="recursive-workspace" ref={host} data-dragging={!!drag} aria-label="Настраиваемое рабочее пространство">
   {!allRects.length&&<div className="recursive-empty">Откройте нужную область в меню «Панели» или выберите готовый вид.</div>}
   {allRects.map(rect=><section className={'recursive-group'+(rect.group.floating?' recursive-floating':'')} key={rect.group.id} data-dock-group={rect.group.id} style={{...position(rect),zIndex:rect.z}} aria-label={'Область: '+rect.group.tabs.map(getTitle).join(', ')}>
    <div className="recursive-tabs" role="tablist" aria-label="Панели области">{rect.group.tabs.map(id=><div className={'recursive-tab'+(rect.group.active===id?' active':'')} key={id}>
     <button type="button" role="tab" aria-selected={rect.group.active===id} aria-controls={'dock-panel-'+id} id={'dock-tab-'+id} data-tab-panel={id} tabIndex={rect.group.active===id?0:-1} onPointerDown={e=>startMove(e,id)} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel} onClick={e=>{if(e.detail===0)activate(rect.group,id)}} onKeyDown={e=>tabKey(e,rect.group,id)} title="Перенести вкладку: потяните к краю области. Alt + стрелка — разделить рядом с другой областью."><GripVertical size={12}/>{getTitle(id)}</button>
     {rect.group.floating?<button data-close-panel type="button" aria-label={'Закрепить панель '+getTitle(id)} title="Закрепить во вкладке" onClick={()=>dock(id)}><PanelsTopLeft size={12}/></button>:<button data-close-panel type="button" aria-label={'Открыть отдельно панель '+getTitle(id)} title="Отдельное окно" onClick={()=>floatPanel(id)}><ExternalLink size={11}/></button>}
     <button data-close-panel type="button" aria-label={'Скрыть панель '+getTitle(id)} disabled={!rect.group.floating&&visible.length===1&&rect.group.tabs.length===1&&!floating.length} onClick={()=>closePanel(id)}><X size={12}/></button>
    </div>)}</div>
   </section>)}
   {/* Stable keyed siblings are intentional: moving, closing or tabbing must never
       unmount ObserverCall (its cleanup ends the call) or discard an SQL draft. */}
   {registry.map(panel=>{const rect=allRects.find(r=>r.group.active===panel.id),bounds=rect?{...rect,y:rect.y+34,height:Math.max(0,rect.height-34)}:{x:0,y:0,width:0,height:0};return <section key={panel.id} id={'dock-panel-'+panel.id} data-panel={panel.id} data-placement={rect?(rect.group.floating?'float':'docked'):'hidden'} role="tabpanel" aria-labelledby={'dock-tab-'+panel.id} aria-label={'Панель '+panel.title} hidden={!rect} className={'recursive-panel '+(panel.id==='calls'?'quest-sidebar':panel.id==='mis'?'query-sidebar':'quest-main')} onPointerDown={()=>{if(rect?.group.floating)raiseFloat(panel.id)}} style={{...position(bounds),zIndex:rect?.group.floating?rect.z+1:undefined}}><div className="recursive-panel-content">{panel.content}</div></section>})}
   {layout.dividers.map(divider=><div key={divider.node.id} className={'recursive-divider '+divider.node.axis} style={position(divider)} role="separator" tabIndex={0} aria-orientation={divider.node.axis==='row'?'vertical':'horizontal'} aria-label={'Размер области '+(divider.node.axis==='row'?'по горизонтали':'по вертикали')} aria-valuemin={12} aria-valuemax={88} aria-valuenow={Math.round(divider.node.ratio*100)} data-split-id={divider.node.id} onPointerDown={e=>startResize(e,divider)} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel} onKeyDown={e=>resizeKey(e,divider)} onDoubleClick={()=>store(updateNode(tree,divider.node.id,node=>({...node,ratio:.5})))}/>)}
   {floatingRects.map(rect=><div key={'resize-'+rect.group.id} className="recursive-float-resizer" style={{left:rect.x+rect.width-15,top:rect.y+rect.height-15,zIndex:rect.z+2}} role="separator" tabIndex={0} aria-label={'Размер плавающей панели '+getTitle(rect.group.active)} aria-valuemin={260} aria-valuemax={Math.round(size.width)} aria-valuenow={Math.round(rect.width)} title="Потяните угол. Стрелки меняют ширину и высоту; Home и End — минимальный и максимальный размер." onPointerDown={e=>startFloatResize(e,rect)} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel} onKeyDown={e=>floatResizeKey(e,rect)}/>)}
   {dropPreview&&<div className="recursive-drop-preview" style={position(dropPreview)}>{EDGES[drag.target.edge]} · {getTitle(drag.id)}</div>}
   {drag&&!drag.target&&<div className="recursive-drag-hint" style={{left:clamp(drag.x,0,size.width-220),top:clamp(drag.y,0,size.height-40)}}>Выберите область для панели</div>}
  </div>
  <div className="dock-announcement" role="status" aria-live="polite">{notice}</div>
 </>;
}

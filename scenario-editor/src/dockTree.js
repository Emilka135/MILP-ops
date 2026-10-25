// Layout preferences contain panel identifiers only, never patient data.
export const LAYOUT_KEY = 'clinic-workspace-tree-v2';
let serial = 0;
const uid = prefix => `${prefix}-${Date.now().toString(36)}-${++serial}`;
export const leaf = tabs => ({id:uid('group'),type:'tabs',tabs:[...tabs],active:tabs[0]});
export const split = (axis,first,second,ratio=.5) => ({id:uid('split'),type:'split',axis,ratio,first,second});
export function leaves(tree){return !tree?[]:tree.type==='tabs'?[tree]:[...leaves(tree.first),...leaves(tree.second)]}
export const panelGroup=(tree,id)=>leaves(tree).find(group=>group.tabs.includes(id));
export function updateNode(tree,id,fn){if(!tree)return tree;if(tree.id===id)return fn(tree);if(tree.type==='tabs')return tree;return {...tree,first:updateNode(tree.first,id,fn),second:updateNode(tree.second,id,fn)}}
export function removePanel(tree,id){
 if(!tree)return null;
 if(tree.type==='tabs'){const tabs=tree.tabs.filter(tab=>tab!==id);return tabs.length?{...tree,tabs,active:tabs.includes(tree.active)?tree.active:tabs[0]}:null}
 const first=removePanel(tree.first,id),second=removePanel(tree.second,id);
 return first&&second?{...tree,first,second}:first||second;
}
export function placePanel(tree,id,targetId,edge='center'){
 const target=leaves(tree).find(group=>group.id===targetId);
 if(!target)return tree;
 if(target.tabs.length===1&&target.tabs[0]===id)return tree;
 const next=removePanel(tree,id);
 if(!next)return leaf([id]);
 return updateNode(next,targetId,group=>{
  if(edge==='center')return {...group,tabs:[...group.tabs,id],active:id};
  const incoming=leaf([id]),before=edge==='left'||edge==='top';
  return split(edge==='left'||edge==='right'?'row':'column',before?incoming:group,before?group:incoming);
 });
}
export function preset(name,ids){
 const has=id=>ids.includes(id),one=id=>leaf([id]);
 const center=has('conversation')?'conversation':ids[0];
 if(name==='sql'&&has('sql'))return one('sql');
 if(name==='api'&&has('api'))return one('api');
 if(name==='split'){
  const lower=has('api')?'api':has('route')?'route':ids.find(id=>id!==center);
  const right=has('mis')?'mis':ids.find(id=>id!==center&&id!==lower);
  let tree=lower?split('column',one(center),one(lower)):one(center);
  return right?split('row',tree,one(right),.72):tree;
 }
 let tree=one(center);
 if(has('mis')&&center!=='mis')tree=split('row',tree,one('mis'),.67);
 if(has('calls')&&center!=='calls')tree=split('row',one('calls'),tree,.17);
 return tree;
}
// Reject malformed, excessively nested and duplicate layouts from storage.
export function validateLayout(value,ids){
 const seenPanels=new Set(),seenNodes=new Set();let count=0;
 function visit(node,depth){
  if(!node||typeof node!=='object'||depth>16||++count>63||typeof node.id!=='string'||node.id.length>100||seenNodes.has(node.id))throw Error('Invalid layout');
  seenNodes.add(node.id);
  if(node.type==='tabs'){
   if(!Array.isArray(node.tabs)||!node.tabs.length||node.tabs.length>ids.length)throw Error('Invalid tabs');
   const tabs=node.tabs.filter(id=>ids.includes(id));
   if(!tabs.length)return null;
   for(const id of tabs){if(seenPanels.has(id))throw Error('Duplicate panel');seenPanels.add(id)}
   return {id:node.id,type:'tabs',tabs,active:tabs.includes(node.active)?node.active:tabs[0]};
  }
  if(node.type!=='split'||!['row','column'].includes(node.axis)||!Number.isFinite(node.ratio))throw Error('Invalid split');
  const first=visit(node.first,depth+1),second=visit(node.second,depth+1);
  return first&&second?{id:node.id,type:'split',axis:node.axis,ratio:Math.max(.12,Math.min(.88,node.ratio)),first,second}:first||second;
 }
 try{return visit(value,0)}catch{return null}
}
export function rectangles(tree,width,height,gap=6){
 const groups=[],dividers=[];
 function visit(node,rect){
  if(!node)return;
  if(node.type==='tabs'){groups.push({...rect,group:node});return}
  const horizontal=node.axis==='row',length=horizontal?rect.width:rect.height,available=Math.max(0,length-gap),first=available*node.ratio;
  const a={...rect},b={...rect},divider={...rect,node};
  if(horizontal){a.width=first;b.x+=first+gap;b.width=available-first;divider.x+=first;divider.width=gap}
  else{a.height=first;b.y+=first+gap;b.height=available-first;divider.y+=first;divider.height=gap}
  dividers.push({...divider,parent:rect});visit(node.first,a);visit(node.second,b);
 }
 visit(tree,{x:0,y:0,width,height});return {groups,dividers};
}

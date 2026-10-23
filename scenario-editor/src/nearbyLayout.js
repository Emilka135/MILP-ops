/** Human reading order: previous -> current -> up to four immediate alternatives. */
export function nearbyLayout(graph,centerId,page=0){
 const byId=new Map(graph.nodes.map(node=>[node.id,node]));
 const center=byId.get(centerId)||graph.nodes[0];if(!center)return {...graph,alternatives:0,pages:0};
 const grouped=new Map();
 for(const edge of graph.edges){if(edge.source!==center.id||edge.target===center.id)continue;if(!grouped.has(edge.target))grouped.set(edge.target,[]);grouped.get(edge.target).push(edge)}
 const alternatives=[...grouped.values()];const pages=Math.ceil(alternatives.length/4);
 const chosen=alternatives.slice(Math.max(0,Math.min(page,pages-1))*4,Math.max(0,Math.min(page,pages-1))*4+4);
 const previous=graph.edges.find(edge=>edge.target===center.id&&edge.source!==center.id&&!grouped.has(edge.source))||graph.edges.find(edge=>edge.target===center.id&&edge.source!==center.id);
 const handles=chosen.map(group=>group[0].sourceHandle);
 const nodes=[{...center,position:{x:365,y:180},data:{...center.data,handles}}];
 const edges=[];
 if(previous&&byId.has(previous.source)&&!chosen.some(group=>group[0].target===previous.source)){
  const parent=byId.get(previous.source);nodes.push({...parent,position:{x:20,y:180},data:{...parent.data,compact:true,handles:[previous.sourceHandle]}});edges.push(previous)
 }
 chosen.forEach((group,index)=>{const edge=group[0],next=byId.get(edge.target);if(!next)return;nodes.push({...next,position:{x:755,y:40+index*175},data:{...next.data,compact:true,handles:[]}});edges.push({...edge,label:edge.label+(group.length>1?' (+'+(group.length-1)+')':'')})});
 return {nodes,edges,alternatives:alternatives.length,pages};
}

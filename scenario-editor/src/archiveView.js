export function archiveKey(graph,ref){return Object.hasOwn(graph?.titles||{},ref)||Object.hasOwn(graph?.nodes||{},ref)?ref:ref?.replace(/^demo\./,'')}
export function archiveTitle(call,ref){const graph=call.graph||{},key=archiveKey(graph,ref);return graph.titles?.[key]||graph.nodes?.[key]?.title||ref}
export function branchRef(current,branch){return branch.includes('.')?branch:current.slice(0,current.lastIndexOf('.')+1)+branch}
export function archiveView(call,ref){
 const graph=call.graph||{},key=archiveKey(graph,ref);
 const card=id=>graph.nodes?.[archiveKey(graph,id)]||{id,title:archiveTitle(call,id),lines:[],flow:id?.split('.')[0]};
 const next=(graph.branches?.[key]||[]).map(branch=>({label:'Другой шаг',card:card(branchRef(ref,branch))}));
 const parents=Object.entries(graph.branches||{}).filter(([from,branches])=>branches.some(b=>branchRef(from.includes('.')?from:'demo.'+from,b)===ref)).map(([from])=>card(from.includes('.')?from:'demo.'+from));
 return {current:card(ref),parents,next,revision:graph.version};
}
export function callKind(call){return ({runtime:'Телефонный звонок',audio:'Пробный · голос',text:'Пробный · текст',imported:'Загруженная расшифровка',test:'Пробный · клавиши'})[call.kind||call.call_kind]||'Телефонный звонок'}

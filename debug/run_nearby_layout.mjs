/** Reading-order invariants: bounded neighbors, unique nodes, no global fan-out. */
import assert from 'node:assert/strict';
import {nearbyLayout} from '../scenario-editor/src/nearbyLayout.js';
const nodes=Array.from({length:15},(_,i)=>({id:'n'+i,position:{x:999,y:999},data:{state:{}}}));
const edges=nodes.slice(1).map((node,i)=>({id:'e'+i,source:'n0',sourceHandle:'e:'+i,target:node.id,label:'Вариант '+i}));
const graph={nodes,edges:[...edges,{id:'loop',source:'n0',target:'n0'}]};
for(let page=0;page<4;page++){const reading=nearbyLayout(graph,'n0',page);assert.equal(reading.pages,4);assert.ok(reading.nodes.length<=5);assert.equal(new Set(reading.nodes.map(n=>n.id)).size,reading.nodes.length);assert.ok(reading.edges.length<=4);assert.ok(reading.nodes.slice(1).every(n=>n.position.x>reading.nodes[0].position.x));assert.ok(reading.edges.every(e=>e.source!==e.target))}
assert.equal(nearbyLayout({nodes:[],edges:[]},'missing').nodes.length,0);
console.log('Passed: linear neighbors, bounded alternatives, loops remain in inspector, empty graph');

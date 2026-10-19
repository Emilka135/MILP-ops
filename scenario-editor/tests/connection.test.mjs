import test from 'node:test';
import assert from 'node:assert/strict';
import {connectionChange} from '../src/model.js';

function project() {
  return {flows:[
    {name:'first',states:[
      {name:'start',edges:[{kind:'dtmf',values:['1'],target:'done',sets:{}}],statements:[{kind:'goto',args:{target:'done'}}]},
      {name:'done',edges:[],statements:[]},
      {name:'new_end',edges:[],statements:[]},
    ]},
    {name:'second',states:[{name:'start',edges:[],statements:[]}]},
  ]};
}

function connection(sourceHandle='new', target='first.new_end') {
  return {source:'first.start',sourceHandle,target};
}

test('inspector index exists before a deferred React updater runs', async () => {
  const original=project();
  const change=connectionChange(original,connection());
  assert.deepEqual(change.selection,{flow:'first',node:'start',type:'edge',index:1});
  assert.equal(original.flows[0].states[0].edges.length,1);
  const next=structuredClone(original);
  await new Promise(resolve=>queueMicrotask(()=>{change.apply(next);resolve()}));
  const selected=next.flows[0].states[0].edges[change.selection.index];
  assert.equal(selected.kind,'any');
  assert.equal(selected.target,'new_end');
  assert.equal(original.flows[0].states[0].edges.length,1);
});

test('updater replay produces the same connection and selection', () => {
  const original=project(),change=connectionChange(original,connection());
  const first=structuredClone(original),second=structuredClone(original);
  change.apply(first);change.apply(second);
  assert.deepEqual(first,second);
  assert.equal(change.selection.index,1);
});

test('reconnecting an existing condition preserves its digits and index', () => {
  const next=project(),change=connectionChange(next,connection('e:0'));
  change.apply(next);
  assert.equal(next.flows[0].states[0].edges.length,1);
  assert.deepEqual(next.flows[0].states[0].edges[0],{kind:'dtmf',values:['1'],target:'new_end',sets:{}});
  assert.equal(change.selection.index,0);
});

test('cross-scenario statement keeps a qualified target', () => {
  const next=project(),change=connectionChange(next,connection('s:0','second.start'));
  change.apply(next);
  assert.deepEqual(change.selection,{flow:'first',node:'start',type:'statement',index:0});
  assert.equal(next.flows[0].states[0].statements[0].args.target,'second.start');
});

test('invalid or stale handle fails before queuing any state change', () => {
  for(const handle of ['e:99','s:99','e:-1','invalid']) {
    const original=project(),before=structuredClone(original);
    assert.throws(()=>connectionChange(original,connection(handle)));
    assert.deepEqual(original,before);
  }
  assert.throws(()=>connectionChange(project(),connection('new','first.missing')));
});

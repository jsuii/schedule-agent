'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Node {constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.textContent='';this.disabled=false;}append(...xs){this.children.push(...xs);}replaceChildren(){this.children=[];}setAttribute(k,v){this[k]=v;if(k==='data-locked')this.dataset.locked=v;}}
const roots=new Map();function all(n){return[n,...(n.children||[]).flatMap(all)];}
const document={createElement:t=>new Node(t),createTextNode:t=>({textContent:t}),querySelector:s=>{if(!roots.has(s))roots.set(s,new Node('div'));return roots.get(s);},querySelectorAll:s=>[...roots.values()].flatMap(all).filter(n=>['button','input'].includes(n.tag))};
const sandbox={document,Date,Intl,Set,console};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('scheduler/static/app.js','utf8').split("$('#chat-form').onsubmit")[0],sandbox);
vm.runInContext(`current={timezone:'Asia/Shanghai',proposal:{id:'restore',action:'restore_batch',event:{selected:[{id:'x',title:'X',start:'2026-10-19T10:00+08:00',end:'2026-10-19T11:00+08:00'}],conflicts:[{event:{title:'X'},existing:{title:'Y'},overlap:{start:'2026-10-19T10:00+08:00',end:'2026-10-19T11:00+08:00'}}],feasible_ids:[]}}};busy=true;renderProposal(current.proposal);setBusy(false);`,sandbox);
const confirm=all(roots.get('#proposal')).find(n=>n.tag==='button'&&n.textContent==='确认恢复所选 1 项日程');
assert(confirm);assert(confirm.hidden);assert(confirm.disabled,'restore-conflict confirmation must stay disabled after loading ends');
console.log('Blocked restoration remains disabled; actual render and busy-state functions executed.');
const buttons=all(roots.get('#proposal')).filter(n=>n.tag==='button');
assert(buttons.some(n=>n.textContent==='重新检查原时间'&&!n.disabled));
assert(buttons.some(n=>n.textContent==='保留在回收站'&&!n.disabled));
// Exercise actual request lifecycle with deterministic fetch, no real LLM/network.
sandbox.AbortController=AbortController;sandbox.setTimeout=setTimeout;sandbox.clearTimeout=clearTimeout;
const realRefresh=vm.runInContext('refresh',sandbox);
sandbox.fetch=async()=>({ok:false,json:async()=>({message:'validation failed'})});
(async()=>{
  vm.runInContext('refresh=async()=>{}',sandbox);
  await vm.runInContext("operation('/test',{})",sandbox);
  assert.equal(vm.runInContext('busy',sandbox),false);
  sandbox.fetch=async()=>{throw new Error('network failed');};
  await vm.runInContext("operation('/test',{})",sandbox);
  assert.equal(vm.runInContext('busy',sandbox),false);
  sandbox.fetch=async()=>({ok:true,json:async()=>({status:'error',message:'restore_conflict'})});
  await vm.runInContext("operation('/test',{})",sandbox);
  assert.equal(vm.runInContext('busy',sandbox),false);
  // Server failure is terminal too.
  sandbox.fetch=async()=>({ok:false,json:async()=>({message:'server failed'})});
  await vm.runInContext("operation('/test',{})",sandbox);
  assert.equal(vm.runInContext('busy',sandbox),false);
  vm.runInContext(`current.proposal.event.alternatives=[{id:'slot1',selected:[{title:'X',start:'2026-10-19T11:00+08:00',end:'2026-10-19T12:00+08:00'}]}];renderProposal(current.proposal);setBusy(false);`,sandbox);
  const alternative=all(roots.get('#proposal')).find(n=>n.tag==='button'&&n.textContent.startsWith('改到 '));
  assert(alternative&&!alternative.disabled);
  let late,calls=0;
  sandbox.fetch=()=>{calls++;return new Promise(resolve=>{late=resolve;});};
  const old=vm.runInContext("operation('/old',{})",sandbox);
  await vm.runInContext("operation('/duplicate',{})",sandbox);assert.equal(calls,1);
  vm.runInContext('stopWaiting()',sandbox);
  assert.equal(vm.runInContext('busy',sandbox),false);
  sandbox.fetch=async()=>({ok:true,json:async()=>({status:'committed',message:'NEW receipt'})});
  await vm.runInContext("operation('/new',{})",sandbox);
  late({ok:true,json:async()=>({status:'committed',message:'OLD receipt'})});
  await old;
  const text=all(roots.get('#messages')).map(n=>n.textContent).join(' ');
  assert(text.includes('NEW receipt'));assert(!text.includes('OLD receipt'));
  assert.equal(vm.runInContext('busy',sandbox),false);
  // A late state GET is invalidated by a newer operation, before any DOM write.
  let staleState;
  sandbox.fetch=()=>new Promise(resolve=>{staleState=resolve;});
  const oldState=realRefresh();
  vm.runInContext("++stateEpoch;current={marker:'new'}",sandbox);
  staleState({ok:true,json:async()=>({marker:'old'})});await oldState;
  assert.equal(vm.runInContext('current.marker',sandbox),'new');
  // Timeout aborts a real pending fetch contract and clears busy.
  sandbox.setTimeout=fn=>setTimeout(fn,1);
  sandbox.fetch=(url,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'}))));
  await vm.runInContext("operation('/timeout',{})",sandbox);
  assert.equal(vm.runInContext('busy',sandbox),false);
  console.log('Validation, HTTP/network/conflict, timeout/cancel, duplicate guard and late responses verified.');
})().catch(e=>{console.error(e);process.exitCode=1;});

'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Node {constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.textContent='';}append(...xs){this.children.push(...xs);}replaceChildren(){this.children=[];}setAttribute(k,v){this[k]=v;}focus(){}}
const roots=new Map();const all=n=>[n,...(n.children||[]).flatMap(all)];
const document={createElement:t=>new Node(t),createTextNode:t=>({textContent:t}),querySelector:s=>{if(!roots.has(s))roots.set(s,new Node('div'));return roots.get(s);},querySelectorAll:()=>[...roots.values()].flatMap(all).filter(n=>['button','input'].includes(n.tag))};
let counter=0;const sandbox={document,Date,Intl,Set,console,AbortController,setTimeout,clearTimeout,crypto:{randomUUID:()=>String(++counter)}};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('scheduler/static/app.js','utf8').split("$('#chat-form').onsubmit")[0],sandbox);
vm.runInContext(`current={timezone:'Asia/Shanghai'};renderContexts([{id:'email',title:'邮件',status:'suspended'},{id:'hair',title:'理发',status:'awaiting_clarification'}],'hair');renderSlots({id:'review',title:'理发',range:{start:'2026-10-20T12:00+08:00',end:'2026-10-20T18:00+08:00'},duration_minutes:60,candidates:[{id:'slot1',start:'2026-10-20T15:00+08:00',end:'2026-10-20T16:00+08:00'}]});`,sandbox);
assert(all(roots.get('#tasks')).some(n=>n.textContent==='邮件 · 已挂起'));
assert(all(roots.get('#tasks')).some(n=>n.tag==='button'&&n.textContent==='恢复此任务'));
const option=all(roots.get('#slots')).find(n=>n.tag==='button'&&n.textContent.includes('15:00'));
assert(option&&!option.disabled);
assert(all(roots.get('#slots')).some(n=>n.textContent.includes('尚未添加')));
(async()=>{
  const calls=[];
  sandbox.fetch=async(path,opts)=>{calls.push(path);return{ok:true,json:async()=>({})};};
  vm.runInContext('refresh=async()=>{}',sandbox);
  await option.onclick();assert.equal(calls[0],'/api/slots/review/slot1');
  // Chat cancellation and late results must obey the same generation guard as buttons.
  let lateOld,lateNew;
  sandbox.fetch=()=>new Promise(resolve=>{lateOld=resolve;});
  const old=vm.runInContext("sendText('旧邮件请求')",sandbox);
  vm.runInContext('stopWaiting()',sandbox);
  sandbox.fetch=()=>new Promise(resolve=>{lateNew=resolve;});
  const newer=vm.runInContext("sendText('新的理发请求')",sandbox);
  lateOld({ok:true,json:async()=>({message:'OLD wrong email answer',status:'clarification'})});
  await old;assert.equal(vm.runInContext('busy',sandbox),true,'old finally must not unlock newer request');
  lateNew({ok:true,json:async()=>({message:'NEW haircut slots',status:'clarification'})});
  await newer;assert.equal(vm.runInContext('busy',sandbox),false);
  const text=all(roots.get('#messages')).map(n=>n.textContent).join(' ');
  assert(text.includes('NEW haircut slots'));assert(!text.includes('OLD wrong email answer'));
  vm.runInContext('current.proposal={id:"p"};renderSlots({id:"review"})',sandbox);
  assert(roots.get('#slots').hidden);
  console.log('Contexts, actual slot selection, chat cancellation and old-response isolation passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});

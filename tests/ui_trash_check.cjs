'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Node {constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.textContent='';this.disabled=false;}append(...xs){this.children.push(...xs);}replaceChildren(){this.children=[];}setAttribute(k,v){this[k]=v;if(k==='data-locked')this.dataset.locked=v;}}
const roots=new Map();function all(n){return[n,...(n.children||[]).flatMap(all)];}
const document={createElement:t=>new Node(t),createTextNode:t=>({textContent:t}),querySelector:s=>{if(!roots.has(s))roots.set(s,new Node('div'));return roots.get(s);},querySelectorAll:s=>[...roots.values()].flatMap(all).filter(n=>['button','input'].includes(n.tag))};
const sandbox={document,Date,Intl,Set,console};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('scheduler/static/app.js','utf8').split("$('#chat-form').onsubmit")[0],sandbox);
vm.runInContext(`current={timezone:'Asia/Shanghai',proposal:{id:'restore',action:'restore_batch',event:{selected:[{id:'x',title:'X',start:'2026-10-19T10:00+08:00',end:'2026-10-19T11:00+08:00'}],conflicts:[{event:{title:'X'},existing:{title:'Y'},overlap:{start:'2026-10-19T10:00+08:00',end:'2026-10-19T11:00+08:00'}}],feasible_ids:[]}}};busy=true;renderProposal(current.proposal);setBusy(false);`,sandbox);
const confirm=all(roots.get('#proposal')).find(n=>n.tag==='button'&&n.textContent==='确认恢复所选 1 项日程');
assert(confirm);assert(confirm.disabled,'restore-conflict confirmation must stay disabled after loading ends');
console.log('Blocked restoration remains disabled; actual render and busy-state functions executed.');

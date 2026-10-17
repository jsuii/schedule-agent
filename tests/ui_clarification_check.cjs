'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Node {constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.textContent='';this.value='';}append(...xs){this.children.push(...xs);}replaceChildren(){this.children=[];}setAttribute(k,v){this[k]=v;}}
const roots=new Map(),document={createElement:t=>new Node(t),createTextNode:t=>({textContent:t}),querySelector:s=>{if(!roots.has(s))roots.set(s,new Node('div'));return roots.get(s);}};
const sandbox={document,Date,Intl,Set,console};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('scheduler/static/app.js','utf8').split("$('#chat-form').onsubmit")[0],sandbox);
const data={windows:[{titles:['买菜','吃早餐'],start:'2026-10-18T09:00:00+08:00',end:'2026-10-18T10:00:00+08:00',question:'买菜和吃早餐各需要多久？先做哪项？'}]};
sandbox.data=data;
vm.runInContext("current={timezone:'Asia/Shanghai'};renderClarification(data);",sandbox);
function all(n){return[n,...(n.children||[]).flatMap(all)];}
let nodes=all(roots.get('#clarification'));
assert(nodes.some(n=>n.textContent.includes('尚未添加')));
assert(nodes.some(n=>n.textContent.includes('买菜')&&n.textContent.includes('吃早餐')));
assert(nodes.some(n=>n.textContent.includes('09:00')&&n.textContent.includes('10:00')));
assert(nodes.some(n=>n.tag==='button'&&n.textContent==='补充并继续'));
assert(!nodes.some(n=>n.tag==='button'&&n.textContent==='确认添加'));
const input=nodes.find(n=>n.tag==='input'),form=nodes.find(n=>n.tag==='form');
input.value='先买菜40分钟，再吃早餐20分钟';
vm.runInContext('sendText=text=>{current.sent=text;}',sandbox);
form.onsubmit({preventDefault(){}});
assert.equal(vm.runInContext('current.sent',sandbox),input.value);
vm.runInContext('busy=true;renderClarification(data);',sandbox);
nodes=all(roots.get('#clarification'));assert(nodes.filter(n=>['input','button'].includes(n.tag)).every(n=>n.disabled));
vm.runInContext('renderClarification(null)',sandbox);assert(roots.get('#clarification').hidden);
console.log('Actual clarification renderer: clear status, known range, next step, submit, busy and terminal hide passed.');
vm.runInContext("current.proposal={id:'pending'};renderClarification(data)",sandbox);assert(roots.get('#clarification').hidden);
vm.runInContext("current.proposal=null;current.conflict={id:'conflict'};renderClarification(data)",sandbox);assert(roots.get('#clarification').hidden);

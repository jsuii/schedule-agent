'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Node {constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.hidden=false;this.open=false;} append(...xs){this.children.push(...xs);} replaceChildren(){this.children=[];} setAttribute(k,v){this[k]=v;} }
const roots=new Map(),document={createElement:t=>new Node(t),createTextNode:text=>({textContent:text}),querySelector:s=>{if(!roots.has(s))roots.set(s,new Node('div'));return roots.get(s);}};
const source=fs.readFileSync('scheduler/static/app.js','utf8').split("$('#chat-form').onsubmit")[0];
const sandbox={document,Intl,Date,Set,console};vm.createContext(sandbox);vm.runInContext(source,sandbox);
vm.runInContext(`current={timezone:'Asia/Shanghai'};renderConflict({timezone:'Asia/Shanghai',facts:[{requested:{title:'甜品',start:'2026-10-19T11:00+08:00',end:'2026-10-19T13:00+08:00'},existing:{id:'x',title:'买菜',start:'2026-10-19T12:00+08:00',end:'2026-10-19T12:30+08:00'},overlap:{start:'2026-10-19T12:00+08:00',end:'2026-10-19T12:30+08:00'},question:'technical details'}],plans:[1,2].map(n=>({id:'plan'+n,title:'方案'+n,reason:'减少一次移动。 More prose',events:[{after:{title:'甜品',start:'2026-10-19T13:00+08:00',end:'2026-10-19T15:00+08:00'}}],assumptions:['外部预约未核实']}))});`,sandbox);
function all(node,hidden=false){const h=hidden||node.hidden||(node.tag==='details'&&!node.open);return [{node,hidden}].concat((node.children||[]).flatMap(c=>all(c,h&&c.tag!=='summary')));}
const nodes=all(roots.get('#conflict'));const visible=nodes.filter(x=>!x.hidden).map(x=>x.node);
assert(visible.some(x=>x.tag==='button'&&x.textContent==='采用此方案'));
assert(nodes.some(x=>x.node.tag==='summary'&&x.node.textContent==='其他方案'));
assert(visible.some(x=>x.tag==='input'&&x['aria-label']==='自定义冲突解决方案'));
assert(!visible.some(x=>x.textContent.includes('已验证硬约束')));
assert(!visible.some(x=>x.textContent.includes('technical details')));
assert.equal(nodes.filter(x=>x.node.tag==='details'&&x.node.open).length,0);
console.log('Actual compact renderer: recommendation, collapsed alternatives/details, custom input passed.');
vm.runInContext("message('assistant','提交成功',false,'committed');message('assistant','已取消',false,'cancelled');message('assistant','失败',true,'error');",sandbox);
assert(roots.get('#messages').children.some(x=>x.className.includes('success')));
assert(roots.get('#messages').children.some(x=>x.className.includes('cancelled')));

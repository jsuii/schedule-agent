'use strict';
const $ = s => document.querySelector(s);
let current = null, busy = false, demoIndex = 0;
const demoPrompts = ['明天和老王开会', '下午3点，一小时，地点会议室A', '我明天有什么安排？', '把和老王的会地点改成会议室B', '明天下午3点写周报一小时', '后天下午3点和老王开会一小时', '取消和老王的会'];
function el(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
async function api(path, data) {
  const response=await fetch(path, data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok) throw new Error(result.message || (typeof result.detail==='string'?result.detail:'请求无效，请刷新后重试。'));
  return result;
}
function message(role, text, error=false) {
  const n=el('div',undefined,`message ${role}${error?' error':''}`);
  n.append(el('span',role==='user'?'你':'留白 / ASSISTANT','message-label'),document.createTextNode(text));
  $('#messages').append(n); $('#messages').scrollTop=$('#messages').scrollHeight;
}
function stamp(iso) {
  const d=new Date(iso);
  return new Intl.DateTimeFormat('zh-CN',{timeZone:current?.timezone||'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d);
}
function dateKey(iso) { return new Intl.DateTimeFormat('en-CA',{timeZone:current?.timezone||'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(iso)); }
function detail(e) { return `${e.title}\n${stamp(e.start)} → ${stamp(e.end)}\n地点：${e.location||'未填写'}${e.notes?'\n备注：'+e.notes:''}`; }
function renderEvents() {
  const root=$('#events'), selected=$('#date').value;
  root.replaceChildren();
  const events=(current?.events||[]).filter(e=>!selected || dateKey(e.start)<=selected && dateKey(new Date(new Date(e.end).getTime()-1).toISOString())>=selected);
  $('#count').textContent=events.length; $('#date-label').textContent=selected||'所有安排';
  if(!events.length){const empty=el('div',undefined,'empty');empty.append(el('span','◌','empty-symbol'),document.createTextNode(selected?'这一天暂时没有安排。':'日历还是空的。'),el('br'),document.createTextNode('从左侧的一句话开始。'));root.append(empty);return;}
  for(const e of events){const card=el('article',undefined,'event-card');card.append(el('div',`${stamp(e.start)} — ${stamp(e.end)}`,'event-time'),el('h3',e.title));if(e.location)card.append(el('p','⌖ '+e.location));if(e.notes)card.append(el('p',e.notes));root.append(card);}
  if(current.events_truncated)root.append(el('p','仅显示前500条，请使用对话按日期检索。'));
}
function renderProposal(p) {
  const root=$('#proposal');root.replaceChildren();root.hidden=!p;if(!p)return;
  const card=el('section',undefined,'proposal');
  card.append(el('h3',`${({create:'添加',update:'修改',delete:'删除'})[p.action]}日程 · 等待你的确认`));
  if(p.before && p.action==='update')card.append(el('pre','原日程\n'+detail(p.before)));
  card.append(el('pre',(p.action==='update'?'修改为\n':'')+detail(p.event)),el('p','确认后才会写入；15分钟内有效。'));
  const actions=el('div',undefined,'actions'),yes=el('button','确认'+({create:'添加',update:'修改',delete:'删除'})[p.action],'primary'),no=el('button','取消','secondary');
  for(const [b,approve] of [[yes,true],[no,false]]){b.disabled=busy;b.onclick=async()=>{if(busy)return;setBusy(true);try{const r=await api('/api/proposals/'+p.id,{approve});message('assistant',r.message,r.status==='error');await refresh();}catch(e){message('assistant',e.message,true);await refresh();}finally{setBusy(false);}};actions.append(b);}
  card.append(actions);root.append(card);
}
function renderCandidates(list, visible) {
  const root=$('#candidates');root.replaceChildren();root.hidden=!visible||list.length<2;if(root.hidden)return;
  root.append(el('p','找到多个候选，请明确选择后再继续。','candidate-title'));
  for(const e of list){const b=el('button',detail(e),'candidate');b.disabled=busy;b.onclick=async()=>{if(busy)return;try{await api('/api/select',{event_id:e.id});await sendText('请继续处理我选择的这条日程');}catch(err){message('assistant',err.message,true);}};root.append(b);}
}
function renderTrace(trace) {
  const root=$('#trace');root.replaceChildren();if(!trace?.length)return;
  const d=el('details'), list=el('ol');d.append(el('summary',`本轮使用了 ${trace.length} 次工具`));
  const labels={search_events:'检索日程',find_slots:'寻找空档',propose_create:'准备添加',propose_update:'准备修改',propose_delete:'准备删除',ask_user:'请求补充'};
  for(const t of trace){const code=t.result.error;list.append(el('li',`${labels[t.tool]||t.tool} · ${code?'未执行（'+code+'）':t.result.proposal?'等待确认':t.result.question?'等待回答':'已返回结果'}`));}d.append(list);root.append(d);
}
async function refresh(initial=false) {
  current=await api('/api/state');$('#timezone').textContent=current.timezone;
  if(current.demo)demoIndex=Math.max(0,...current.history.filter(m=>m.role==='user').map(m=>demoPrompts.indexOf(m.content)+1));
  $('#mode').textContent=current.demo?'离线演示 · 模型替身':current.model_configured?'模型已配置':'模型待配置';
  $('#model-info').textContent=current.demo?'固定脚本模型替身 · 不调用真实 LLM':current.model_configured?'当前模型：'+current.model:'配置模型后可使用自然语言安排日程';
  const notice=$('#notice');notice.replaceChildren();notice.hidden=current.model_configured&&!current.demo;
  if(current.demo){notice.append(document.createTextNode('固定脚本演示：没有调用真实 LLM。点击下方步骤，可验证真实工具、数据库和确认流程。确认/取消提案后再继续。'));
    const b=el('button',demoIndex<demoPrompts.length?'演示下一步：'+demoPrompts[demoIndex]:'演示步骤已结束');b.id='demo-next';b.disabled=demoIndex>=demoPrompts.length||busy||!!current.proposal;b.onclick=()=>sendText(demoPrompts[demoIndex++]);notice.append(b);
  }else if(!current.model_configured)notice.textContent='尚未配置真实模型。网页与本地日历已就绪；配置 .env 后重启即可使用自然语言，或按 README 启动独立的固定脚本演示。';
  if(initial){$('#messages').replaceChildren();for(const m of current.history)message(m.role,m.content);}
  renderEvents();renderProposal(current.proposal);renderCandidates(current.candidates,current.candidates.length>1&&!current.proposal);
}
function setBusy(value){busy=value;$('#send').disabled=value;$('#new-chat').disabled=value;$('#input').disabled=value;$('#processing').textContent=value?'正在处理，请稍候…':'Enter 发送 · Shift + Enter 换行';document.querySelectorAll('#proposal button,#candidates button').forEach(b=>b.disabled=value);if($('#demo-next'))$('#demo-next').disabled=value||demoIndex>=demoPrompts.length||!!current?.proposal;}
async function sendText(text) {
  text=text?.trim();if(!text||busy)return;
  message('user',text);$('#input').value='';setBusy(true);$('#candidates').hidden=true;
  try{const result=await api('/api/chat',{text,request_id:crypto.randomUUID()});message('assistant',result.message,result.status==='error');renderTrace(result.trace);await refresh();renderCandidates(result.candidates||[],result.status==='clarification');}
  catch(error){message('assistant',error.message,true);try{await refresh();}catch{/* Keep visible network error. */}}
  finally{setBusy(false);$('#input').focus();if(current?.demo)await refresh();}
}
$('#chat-form').onsubmit=e=>{e.preventDefault();sendText($('#input').value);};
$('#input').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();sendText($('#input').value);}};
$('#date').onchange=renderEvents;$('#all-dates').onclick=()=>{$('#date').value='';renderEvents();};
$('#refresh').onclick=()=>refresh().catch(e=>message('assistant',e.message,true));
$('#new-chat').onclick=async()=>{if(busy)return;try{await api('/api/session',{});$('#trace').replaceChildren();demoIndex=0;await refresh(true);}catch(e){message('assistant',e.message,true);}};
for(const b of document.querySelectorAll('[data-prompt]'))b.onclick=()=>{$('#input').value=b.dataset.prompt;$('#input').focus();};
refresh(true).catch(e=>message('assistant','无法连接本地服务：'+e.message,true));

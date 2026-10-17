'use strict';
const $ = s => document.querySelector(s);
let current = null, busy = false, demoIndex = 0;
let actionEpoch=0, stateEpoch=0, trashEpoch=0, activeAction=null;
const demoPrompts = ['明天和老王开会', '下午3点，一小时，地点会议室A', '我明天有什么安排？', '把和老王的会地点改成会议室B', '明天下午3点写周报一小时', '后天下午3点和老王开会一小时', '取消和老王的会'];
function el(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
async function api(path, data, signal) {
  const controller=new AbortController();
  const abort=()=>controller.abort();
  if(signal?.aborted)abort();else signal?.addEventListener('abort',abort,{once:true});
  const timer=setTimeout(abort,path==='/api/chat'?100000:20000);
  try {
    const response=await fetch(path,{...(data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}),signal:controller.signal});
    const result=await response.json();
    if(!response.ok)throw new Error(result.message || (typeof result.detail==='string'?result.detail:'请求无效，请刷新后重试。'));
    return result;
  } catch(error) {
    if(error.name==='AbortError')throw new Error('请求已停止或超时，请刷新核对结果后重试；服务器可能已完成操作。');
    throw error;
  } finally {clearTimeout(timer);signal?.removeEventListener('abort',abort);}
}
function message(role, text, error=false, status='') {
  const n=el('div',undefined,`message ${role}${error?' error':''}${status==='committed'?' success':status==='cancelled'?' cancelled':''}`);
  n.append(el('span',role==='user'?'你':'留白 / ASSISTANT','message-label'),document.createTextNode(text));
  $('#messages').append(n); $('#messages').scrollTop=$('#messages').scrollHeight;
}
function stamp(iso) {
  const d=new Date(iso);
  return new Intl.DateTimeFormat('zh-CN',{timeZone:current?.timezone||'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d);
}
function dateKey(iso) { return new Intl.DateTimeFormat('en-CA',{timeZone:current?.timezone||'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(iso)); }
const constraintNames={unknown:'未说明',high:'高',medium:'中',low:'低',fixed:'固定',movable:'可移动',shortenable:'可缩短',splittable:'可拆分',noninterruptible:'不可打断',pausable:'可暂停',resumable:'可暂停恢复',parallel:'可并行'};
function constraint(value){return constraintNames[value]||'未说明';}
function detail(e) { return `${e.title}\n${stamp(e.start)} → ${stamp(e.end)}\n地点：${e.location||'未填写'}${e.notes?'\n备注：'+e.notes:''}\n约束：重要性 ${constraint(e.importance)} / 时间 ${constraint(e.flexibility)} / 可打断性 ${constraint(e.interruptibility)}${e.keep_time?' / 必须保留时间':''}${e.overlap_allowed?' / 已允许重叠':''}${e.coexistence_verified?' / 注意力与通勤已核实':''}`; }
function renderEvents() {
  const root=$('#events'), selected=$('#date').value;
  root.replaceChildren();
  const events=(current?.events||[]).filter(e=>!selected || dateKey(e.start)<=selected && dateKey(new Date(new Date(e.end).getTime()-1).toISOString())>=selected);
  $('#count').textContent=events.length; $('#date-label').textContent=selected||'所有安排';
  if(!events.length){const empty=el('div',undefined,'empty');empty.append(el('span','◌','empty-symbol'),document.createTextNode(selected?'这一天暂时没有安排。':'日历还是空的。'),el('br'),document.createTextNode('从左侧的一句话开始。'));root.append(empty);return;}
  for(const e of events){const card=el('article',undefined,'event-card');card.append(el('div',`${stamp(e.start)} — ${stamp(e.end)}`,'event-time'),el('h3',e.title));if(e.location)card.append(el('p','⌖ '+e.location));if(e.notes)card.append(el('p',e.notes));root.append(card);}
  if(current.events_truncated)root.append(el('p','仅显示前500条，请使用对话按日期检索。'));
}
async function operation(path,data) {
  if(busy)return;
  const epoch=++actionEpoch; ++stateEpoch; ++trashEpoch;
  const controller=new AbortController();activeAction=controller;setBusy(true);
  try {
    const result=await api(path,data,controller.signal);
    if(epoch!==actionEpoch)return;
    if(result.message)message('assistant',result.message,result.status==='error',result.status);
    await refresh();
  } catch(error) {
    if(epoch!==actionEpoch)return;
    message('assistant',error.message,true);
    try{await refresh();}catch{/* Keep the original error visible and allow retry. */}
  } finally {if(epoch===actionEpoch){activeAction=null;setBusy(false);}}
}
function stopWaiting(){
  if(!activeAction)return;
  ++actionEpoch;++stateEpoch;++trashEpoch;activeAction.abort();activeAction=null;setBusy(false);
  message('assistant','已停止等待；服务器可能已完成操作，请刷新核对后再继续。');
  refresh().catch(e=>message('assistant',e.message,true));
}
function restoreRefs(events){return events.map(e=>({id:e.id,version:e.version,deletion_batch_id:e.deletion_batch_id}));}
function renderBatchOperation(p,root) {
  const data=p.event,isCancel=p.action==='cancel_batch',card=el('section',undefined,'proposal batch-review');
  const count=data.selected.length;
  card.append(el('h3',isCancel?`取消所选 ${count} 项日程？`:data.conflicts?.length?'恢复时间冲突 · 请选择方案':`恢复所选 ${count} 项日程？`));
  if(isCancel)card.append(el('p',`${stamp(data.range.start)}–${stamp(data.range.end)} · ${current.timezone}`));
  const list=el('div',undefined,'batch-list');
  if(isCancel){
    const selected=new Set(data.selected.map(e=>e.id));
    for(const e of data.snapshot){const row=el('label',undefined,'batch-row'),check=el('input');check.type='checkbox';check.checked=selected.has(e.id);check.setAttribute('aria-label','取消 '+e.title+' '+stamp(e.start));check.value=e.id;check.disabled=busy;
      check.onchange=()=>{const ids=Array.from(root.querySelectorAll('input[type="checkbox"]')).filter(x=>x.checked).map(x=>x.value);if(!ids.length){check.checked=true;message('assistant','至少选择一项；全部保留请点击取消。');return;}operation('/api/cancellations/'+p.id+'/selection',{selected_ids:ids});};row.append(check,el('span',`${e.title} · ${stamp(e.start)}–${stamp(e.end)}`));list.append(row);}
    if(data.unresolved_exclusion?.length)card.append(el('p','保留对象需要明确，请取消勾选要保留的具体日程。','error'));
  }else{for(const e of data.selected)list.append(el('p',`${e.title} · ${stamp(e.start)}–${stamp(e.end)}`));}
  card.append(list);
  if(!isCancel && data.conflicts?.length){
    card.append(el('p','存在恢复冲突，尚未恢复。','error'));
    for(const f of data.conflicts)card.append(el('p',`${f.event.title} 与 ${f.existing.title}：${stamp(f.overlap.start)}–${stamp(f.overlap.end)}`));
    const recheck=el('button','重新检查原时间','secondary');recheck.disabled=busy;recheck.onclick=()=>operation('/api/trash/restore/'+p.id+'/options/original',{});card.append(recheck);
    card.append(el('p','原时间仍有硬冲突时不能恢复。选择以下时间后，还需核对并确认。'));
    for(const option of data.alternatives||[]){const choice=el('button','改到 '+option.selected.map(e=>e.title+' · '+stamp(e.start)+'–'+stamp(e.end)).join('；'),'secondary');choice.disabled=busy;choice.onclick=()=>operation('/api/trash/restore/'+p.id+'/options/'+option.id,{});card.append(choice);}
    if(!data.alternatives?.length)card.append(el('p','当前约束下暂无可用的整体改期方案；可保留在回收站或单独选择其他记录。'));
    if(data.feasible_ids.length){const subset=el('button',`仅准备恢复可行的 ${data.feasible_ids.length} 项`,'secondary');subset.disabled=busy;subset.onclick=()=>operation('/api/trash/restore',{selected:restoreRefs(data.selected.filter(e=>data.feasible_ids.includes(e.id)))});card.append(subset);}
  }
  const actions=el('div',undefined,'actions'),yes=el('button',`确认${isCancel?'取消':'恢复'}所选 ${count} 项日程`,'primary'),no=el('button',!isCancel&&data.conflicts?.length?'保留在回收站':'取消','secondary');
  const locked=!!(isCancel?data.unresolved_exclusion?.length:data.conflicts?.length);yes.setAttribute('data-locked',String(locked));yes.disabled=busy||locked;yes.hidden=!isCancel&&locked;no.disabled=busy;
  for(const [button,approve] of [[yes,true],[no,false]]){button.onclick=()=>operation('/api/proposals/'+p.id,{approve});actions.append(button);}
  card.append(el('p',isCancel?'确认后移至回收站，7天内可恢复。':data.conflicts?.length?'选择方案不会立即恢复；也可保留在回收站。':data.adjustments?'核对改期后的时间，再确认恢复。':'核对后确认，恢复保留原时间。'),actions);root.append(card);
}
function renderUndo(undo){const root=$('#undo');root.replaceChildren();root.hidden=!undo;if(!undo)return;root.append(el('span',`最近取消的 ${undo.count} 项可恢复 · `));const button=el('button','撤销最近删除','quiet');button.disabled=busy;button.onclick=()=>operation('/api/undo/'+undo.operation_id,{});root.append(button);}
async function loadTrash(){const epoch=++trashEpoch,action=actionEpoch;const root=$('#trash-events');const data=await api('/api/trash');if(epoch!==trashEpoch||action!==actionEpoch)return;root.replaceChildren();if(!data.events.length){root.append(el('p','回收站为空。'));return;}
  const batches=new Set();for(const e of data.events){const row=el('article',undefined,'trash-row');row.append(el('strong',e.title),el('p',stamp(e.start)+'–'+stamp(e.end)),el('p','删除：'+stamp(e.deleted_at)+' · 到期：'+stamp(e.expires_at)));const restore=el('button','恢复此项','secondary');restore.setAttribute('data-locked',String(new Date(e.expires_at).getTime()<=Date.now()));restore.disabled=busy||new Date(e.expires_at).getTime()<=Date.now();restore.onclick=()=>operation('/api/trash/restore',{selected:restoreRefs([e])});row.append(restore);root.append(row);batches.add(e.deletion_batch_id);}
  for(const id of batches){const b=el('button','恢复这一删除批次 · '+stamp(data.events.find(e=>e.deletion_batch_id===id).deleted_at),'secondary');b.disabled=busy;b.onclick=()=>operation('/api/trash/batches/'+id,{});root.append(b);}
  if(data.truncated)root.append(el('p','仅显示前1000项，可用对话筛选回收站。'));
}
function renderProposal(p) {
  const root=$('#proposal');root.replaceChildren();root.hidden=!p;if(!p)return;
  if(p.action==='cancel_batch'||p.action==='restore_batch'){renderBatchOperation(p,root);return;}
  const card=el('section',undefined,'proposal');
  card.append(el('h3',`${({create:'添加',update:'修改',delete:'删除',batch:'统一调整'})[p.action]}日程 · 等待你的确认`));
  if(p.before && p.action==='update')card.append(el('pre','原日程\n'+detail(p.before)));
  if(p.action==='batch'){for(const item of p.event.preview){if(item.before)card.append(el('pre','原安排\n'+detail(item.before)));card.append(el('pre','调整后\n'+detail(item.after)));}card.append(el('p','统一确认后原子执行；仅本地日历，外部预约未同步。'));}
  else card.append(el('pre',(p.action==='update'?'修改为\n':'')+detail(p.event)),el('p','确认后才会写入；15分钟内有效。'));
  const actions=el('div',undefined,'actions'),yes=el('button','确认'+({create:'添加',update:'修改',delete:'删除',batch:'统一调整'})[p.action],'primary'),no=el('button','取消','secondary');
  for(const [b,approve] of [[yes,true],[no,false]]){b.disabled=busy;b.onclick=()=>operation('/api/proposals/'+p.id,{approve});actions.append(b);}
  card.append(actions);root.append(card);
}
const phaseNames={new:'新任务',parsing:'正在整理',awaiting_clarification:'待补充或选择',ready_for_proposal:'准备确认',awaiting_confirmation:'等待确认',executing:'正在执行',completed:'已完成',cancelled:'已取消',failed:'未完成，可重试',suspended:'已挂起'};
function renderContexts(list,activeId){
  const root=$('#tasks');root.replaceChildren();const open=(list||[]).filter(c=>!['completed','cancelled'].includes(c.status));root.hidden=!open.length;if(root.hidden)return;
  const panel=el('details');panel.open=true;panel.append(el('summary','当前与挂起的任务'));
  for(const context of open){const row=el('article',undefined,'task-context');row.append(el('p',`${context.id===activeId?'当前：':''}${context.title} · ${phaseNames[context.status]||context.status}`));
    if(context.id!==activeId||!current?.proposal){const resume=el('button','恢复此任务','secondary');resume.disabled=busy;resume.onclick=()=>operation('/api/contexts/'+context.id+'/resume',{});row.append(resume);}
    const cancel=el('button','取消此任务','quiet');cancel.disabled=busy;cancel.onclick=()=>operation('/api/contexts/'+context.id+'/cancel',{});row.append(cancel);panel.append(row);
  }root.append(panel);
}
function renderSlots(review){
  const root=$('#slots');root.replaceChildren();root.hidden=!review||!!current?.proposal;if(root.hidden)return;
  const card=el('section',undefined,'proposal');card.append(el('h3',review.title+' · 选择合适时间'),el('p',`${stamp(review.range.start)}–${stamp(review.range.end)} · ${review.duration_minutes}分钟`));
  if(!review.candidates.length)card.append(el('p','此范围没有可用空档，请放宽时间或换一天。'));
  for(const option of review.candidates){const b=el('button',stamp(option.start)+'–'+stamp(option.end),'secondary');b.disabled=busy;b.onclick=()=>operation('/api/slots/'+review.id+'/'+option.id,{});card.append(b);}
  card.append(el('p','尚未添加。选择时会重新检查冲突，之后还需确认。'));
  const form=el('form',undefined,'conflict-custom'),input=el('input'),button=el('button','调整范围','primary');input.required=true;input.maxLength=4000;input.placeholder='例如：三点以后，或换一天…';input.setAttribute('aria-label','调整可用时间范围');input.disabled=busy;button.disabled=busy;form.append(input,button);form.onsubmit=e=>{e.preventDefault();sendText(input.value);};card.append(form);root.append(card);
}
function renderClarification(c) {
  const root=$('#clarification');root.replaceChildren();root.hidden=!c||!!current?.proposal||!!current?.conflict;if(root.hidden)return;
  const card=el('section',undefined,'proposal clarification-card');card.append(el('h3','尚未添加 · 请补充安排'));
  for(const window of c.windows){card.append(el('p',window.titles.join('、')),el('p',stamp(window.start)+'–'+stamp(window.end)),el('p',window.question));}
  card.append(el('p','补充后会生成待确认清单；核对并点击确认后，才会添加到日历。'));
  const form=el('form',undefined,'conflict-custom'),input=el('input'),button=el('button','补充并继续','primary');
  input.type='text';input.required=true;input.maxLength=4000;input.placeholder='告诉我各需多久、先做哪项…';input.setAttribute('aria-label','补充时长和顺序');input.disabled=busy;button.disabled=busy;
  form.append(input,button);form.onsubmit=e=>{e.preventDefault();sendText(input.value);};card.append(form);root.append(card);
}
function renderConflict(c) {
  const root=$('#conflict');root.replaceChildren();root.hidden=!c||!!current?.proposal;if(root.hidden)return;
  const panel=el('section',undefined,'conflict-panel');panel.append(el('h3','时间冲突'),el('span','尚未保存','conflict-status'));
  for(const f of c.facts){const minutes=Math.round((new Date(f.overlap.end)-new Date(f.overlap.start))/60000);
    panel.append(el('p',`${f.requested.title} 与 ${f.existing.title} · ${stamp(f.overlap.start)}–${stamp(f.overlap.end)}（${minutes}分钟）${f.classification==='hard_conflict'?' · 需错开':''}`,'overlap'));}
  function planCard(p,recommended){const box=el('article',undefined,'resolution-plan');box.append(el('h4',(recommended?'推荐：':'')+p.title),el('p',p.reason.split('。')[0]+'。'));
    for(const e of p.events)box.append(el('p',`${e.after.title}：${stamp(e.after.start)}–${stamp(e.after.end)}`));
    const details=el('details');details.append(el('summary','查看调整详情'));
    for(const e of p.events){if(e.before)details.append(el('pre','原：'+detail(e.before)));details.append(el('pre','调整后：'+detail(e.after)));}
    details.append(el('p',p.assumptions.join(' ')));box.append(details);
    const b=el('button',recommended?'采用此方案':'选择此方案',recommended?'primary':'secondary');b.disabled=busy;b.onclick=()=>operation('/api/plans/'+p.id,{});box.append(b);return box;}
  const recommended=c.plans.find(p=>p.recommended)||c.plans[0];if(recommended)panel.append(planCard(recommended,true));
  else panel.append(el('p','当前约束下没有可行方案，请说明可调整的时间。'));
  const alternatives=el('details',undefined,'conflict-alternatives');alternatives.append(el('summary','其他方案'));
  for(const p of c.plans.filter(p=>p!==recommended))alternatives.append(planCard(p,false));
  if(c.plans.length<2)alternatives.append(el('p','暂无其他已验证方案，可以在下方自定义。'));
  const facts=el('details');facts.append(el('summary','冲突详情'));
  for(const f of c.facts){facts.append(el('pre',detail(f.requested)+'\n'+detail(f.existing)),el('p',f.question));}facts.append(el('p','时区：'+c.timezone+' · 方案15分钟有效'));alternatives.append(facts);panel.append(alternatives);
  const form=el('form',undefined,'conflict-custom'),input=el('input'),button=el('button','发送','primary');input.type='text';input.required=true;input.maxLength=4000;input.placeholder='输入你希望如何调整…';input.setAttribute('aria-label','自定义冲突解决方案');input.disabled=busy;button.disabled=busy;form.append(input,button);form.onsubmit=e=>{e.preventDefault();sendText(input.value);};panel.append(form);root.append(panel);
}
function renderCandidates(list, visible) {
  const root=$('#candidates');root.replaceChildren();root.hidden=!visible||list.length<2;if(root.hidden)return;
  root.append(el('p','找到多个候选，请明确选择后再继续。','candidate-title'));
  for(const e of list){const b=el('button',detail(e),'candidate');b.disabled=busy;b.onclick=async()=>{if(busy)return;try{await api('/api/select',{event_id:e.id});await sendText('请继续处理我选择的这条日程');}catch(err){message('assistant',err.message,true);}};root.append(b);}
}
function renderTrace(trace) {
  const root=$('#trace');root.replaceChildren();if(!trace?.length)return;
  const d=el('details'), list=el('ol');d.append(el('summary',`本轮使用了 ${trace.length} 次工具`));
  const labels={search_events:'检索日程',find_slots:'寻找空档',propose_create:'准备添加',propose_update:'准备修改',propose_delete:'准备删除',propose_cancel_range:'准备批量取消',search_trash:'查看回收站',propose_restore:'准备恢复',task_context:'切换或继续任务',schedule_flexible:'寻找可选时段',stage_task:'整理多项安排',propose_resolution:'准备统一调整',ask_user:'请求补充'};
  for(const t of trace){const code=t.result.error;list.append(el('li',`${labels[t.tool]||t.tool} · ${code?'未执行（'+code+'）':t.result.proposal?'等待确认':t.result.question?'等待回答':'已返回结果'}`));}d.append(list);root.append(d);
}
async function refresh(initial=false) {
  const epoch=++stateEpoch,action=actionEpoch,data=await api('/api/state');if(epoch!==stateEpoch||action!==actionEpoch)return;
  current=data;$('#timezone').textContent=current.timezone;
  if(current.demo)demoIndex=Math.max(0,...current.history.filter(m=>m.role==='user').map(m=>demoPrompts.indexOf(m.content)+1));
  $('#mode').textContent=current.demo?'离线演示 · 模型替身':current.model_configured?'模型已配置':'模型待配置';
  $('#model-info').textContent=current.demo?'固定脚本模型替身 · 不调用真实 LLM':current.model_configured?'当前模型：'+current.model:'配置模型后可使用自然语言安排日程';
  const notice=$('#notice');notice.replaceChildren();notice.hidden=current.model_configured&&!current.demo;
  if(current.demo){notice.append(document.createTextNode('固定脚本演示：没有调用真实 LLM。点击下方步骤，可验证真实工具、数据库和确认流程。确认/取消提案后再继续。'));
    const b=el('button',demoIndex<demoPrompts.length?'演示下一步：'+demoPrompts[demoIndex]:'演示步骤已结束');b.id='demo-next';b.disabled=demoIndex>=demoPrompts.length||busy||!!current.proposal;b.onclick=()=>sendText(demoPrompts[demoIndex++]);notice.append(b);
  }else if(!current.model_configured)notice.textContent='尚未配置真实模型。网页与本地日历已就绪；配置 .env 后重启即可使用自然语言，或按 README 启动独立的固定脚本演示。';
  if(initial){$('#messages').replaceChildren();for(const m of current.history)message(m.role,m.content);}
  renderUndo(current.undo);if($('#trash-panel').open)await loadTrash();if(epoch!==stateEpoch||action!==actionEpoch)return;renderEvents();renderContexts(current.contexts,current.active_context_id);renderSlots(current.slot_review);renderClarification(current.clarification);renderConflict(current.conflict);renderProposal(current.proposal);renderCandidates(current.candidates,current.candidates.length>1&&!current.proposal);
}
function setBusy(value){busy=value;if($('#stop-waiting'))$('#stop-waiting').hidden=!value||!activeAction;$('#send').disabled=value;$('#new-chat').disabled=value;$('#input').disabled=value;$('#processing').textContent=value?'正在处理，请稍候…':'Enter 发送 · Shift + Enter 换行';document.querySelectorAll('#tasks button,#slots button,#slots input,#clarification button,#clarification input,#proposal button,#candidates button,#conflict button,#conflict input,#undo button,#trash-panel button,#proposal input').forEach(b=>b.disabled=value||b.dataset?.locked==='true');if($('#demo-next'))$('#demo-next').disabled=value||demoIndex>=demoPrompts.length||!!current?.proposal;}
async function sendText(text) {
  text=text?.trim();if(!text||busy)return;
  const epoch=++actionEpoch,requestId=crypto.randomUUID();++stateEpoch;++trashEpoch;
  const controller=new AbortController();activeAction=controller;
  message('user',text);$('#input').value='';setBusy(true);$('#candidates').hidden=true;
  try {
    const result=await api('/api/chat',{text,request_id:requestId},controller.signal);
    if(epoch!==actionEpoch||(result.request_id&&result.request_id!==requestId))return;
    message('assistant',result.message,result.status==='error');renderTrace(result.trace);await refresh();
    if(epoch!==actionEpoch)return;
    if(result.view==='trash'){$('#trash-panel').open=true;await loadTrash();}
    if(epoch!==actionEpoch)return;
    renderCandidates(result.candidates||[],result.status==='clarification');
  } catch(error) {
    if(epoch!==actionEpoch)return;
    message('assistant',error.message,true);try{await refresh();}catch{/* Keep visible network error. */}
  } finally {
    if(epoch===actionEpoch){activeAction=null;setBusy(false);$('#input').focus();if(current?.demo)await refresh();}
  }
}
if($('#stop-waiting'))$('#stop-waiting').onclick=stopWaiting;
$('#trash-panel').ontoggle=()=>{if($('#trash-panel').open)loadTrash().catch(e=>message('assistant',e.message,true));};
$('#chat-form').onsubmit=e=>{e.preventDefault();sendText($('#input').value);};
$('#input').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();sendText($('#input').value);}};
$('#date').onchange=renderEvents;$('#all-dates').onclick=()=>{$('#date').value='';renderEvents();};
$('#refresh').onclick=()=>refresh().catch(e=>message('assistant',e.message,true));
$('#new-chat').onclick=async()=>{if(busy)return;++actionEpoch;++stateEpoch;++trashEpoch;setBusy(true);try{await api('/api/session',{});$('#trace').replaceChildren();demoIndex=0;await refresh(true);}catch(e){message('assistant',e.message,true);}finally{setBusy(false);}};
for(const b of document.querySelectorAll('[data-prompt]'))b.onclick=()=>{$('#input').value=b.dataset.prompt;$('#input').focus();};
refresh(true).catch(e=>message('assistant','无法连接本地服务：'+e.message,true));

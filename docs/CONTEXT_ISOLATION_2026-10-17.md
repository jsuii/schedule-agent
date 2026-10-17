# 任务隔离、提醒语义与灵活安排验收

## 诊断依据

先检查Git，保留此前已授权修复及用户未跟踪.vscode；未改.env。修改前完整门禁162通过、2跳过。读取用户实际会话的完整模型tool_calls、工具反馈和state，而非根据界面猜测。

实际失败链路：

1. 提醒请求被模型发成stage_task：email_advisor，title给导师发邮件，start 2026-10-17 15:00，notes确认推荐信事项，没有end。旧日历事件草稿校验于是追问结束或时长。
2. 新理发请求**成功提取**并调用find_slots：2026-10-20 12:00–18:00、60分钟，服务返回真实空档。
3. 模型尝试propose_create理发12–13，被旧email草稿的pending_task门禁拒绝。
4. 错误提示要求用同一草稿stage_task，模型因此把email与haircut合并，旧email缺end继续触发追问。最终state确实含邮件和理发两个无关intent。

主因是单一可变草稿及无条件pending_task门禁没有任务边界/挂起机制；**不是新意图未提取、空档搜索失败，也不是本次原始现象已证实由前端晚到响应造成**。旧SYSTEM又强调继续stage_task而缺少独立任务选择，强化了此错误。提醒问题则是实体类型误配：产品只有占用起止时间的本地日历事件，没有提醒触发、后台通知或邮件发送。

## 实现与状态流转

新增 `scheduler/flow.py`，保留LLM决定语义边界的真实工具调用，代码不匹配“理发/邮件”等词：

- task_context：new独立任务、continue当前任务、resume指定旧任务、cancel指定任务、clarify真实歧义。继续/恢复/取消使用会话真实task_id，不能跨会话或复用完成/取消任务。
- SQLite增加task_contexts表：id、session_id、title、entity、status、snapshot、proposal_id、created/updated。快照保存该任务自己的草稿、共享范围、关系、候选、冲突或提醒内容。新任务先挂起旧上下文，再从空状态开始；恢复只还原目标自己的快照。确认后清除活动state，但其他挂起任务仍在表中。
- 状态沿用现有提案机制：new→parsing→awaiting_clarification；验证后ready_for_proposal→awaiting_confirmation；确认事务内executing→completed或failed。suspended保留非活动任务，任务取消为cancelled。纯取消提案仍允许回到草稿，不等于取消整个任务。
- Agent进入新轮使旧确认暂不可执行；若有未完成草稿或显式上下文，模型必须先选择边界，绕过时返回context_required，不能直接把新事件追加到旧任务。普通无草稿的旧CRUD/候选流程保持兼容，既有待确认操作以ID归档保留。
- continue处理补充/修改时**不复活旧确认**。只有用户显式resume查看旧方案，才可在原期限内恢复其待确认状态；最终确认仍重验版本、任务revision和冲突。
- 取消别的任务仅可恢复本轮开始前实际有效的提案，使用服务端临时paused_proposal_id；此前修改失败而暂停的提案不能被间接复活。两个复审发现均先加失败测试再修复。
- 前端显示“当前与挂起的任务”，每项独立恢复/取消按钮。聊天请求同按钮操作一样使用Abort、操作序号和状态读取序号；旧回复及旧finally不能覆盖/解锁新请求。服务端每会话锁继续串行，返回request_id/context_id，旧请求合法结果仍按原请求持久化。

修改文件：flow.py；storage.py建表及提案/确认状态；tools.py边界/灵活工具及搜索保留当前scope；agent.py边界握手、真实任务目录/恢复快照；app.py状态和选择端点；static/app.js/index.html；demo.py离线替身选中ID读取修正（system后续新增数据不再被误当ID）。没有替换Agent为意图分类器。

## 提醒和灵活安排

提醒通过entity=reminder保存标题、at和notes，明确说明“不支持定时提醒、通知或发送邮件；没有创建提醒”。不造时长、不当成日历事件追问end；可挂起、恢复、取消。用户想转成事件时需要明确的新事件任务。没有新增通知基础设施。

schedule_flexible从实际date和明确duration找空档，复用下午**12:00–18:00**及Calendar.slots（15分钟步长），支持同日not_before下界，日期不匹配拒绝。只返回最多4个已检索候选，无空档则说明并允许放宽范围。地点/备注独立，不继承其他任务字段。模型不能绕过候选选择自行用第一个空档propose/stage；网页选择绑定review/option/context IDs和15分钟期限，选中后重查日历，才创建待确认提案。确认时再次事务校验、写入及读回，重复确认幂等，写入失败回滚。

新HTTP：`/api/contexts/{id}/resume|cancel`，`/api/slots/{review_id}/{option_id}`。GET state增加contexts/active_context_id/slot_review。恢复快照给模型作为数据，HTTP恢复端点不返回原始task_state。

## 自动回归

新增 `tests/test_task_contexts.py` **21项**及实际JS执行的ui_context_check.cjs。初轮9项中8失败（缺工具/隔离/候选机制）、1项成功复现旧pending_task根因；最终覆盖：

- 完整指定六条对话：共享09–10→早餐先/15分钟→买菜45分钟→早餐09–09:15、买菜09:15–10统一确认；羽毛球14–15:30和甜品15:30–17统一确认；提醒说明→独立理发候选。
- 新任务挂起旧字段、直接答复仍同task ID、跨重启存储、显式恢复/取消、歧义一次问、失败后新独立请求。
- 旧提案挂起不可确认、恢复后仍重验冲突、确认后保留别的挂起任务；修改失败后continue/取消别的任务都不能复活旧确认。
- 空档真实性、3点下界、无空档、跨会话/过期/被新清单替代的选择拒绝、模型不能自行挑选、确认前零写入、重复确认与写入故障回滚/failed状态、请求ID回放。
- 实际JS菜单/候选选择路径、取消聊天等待、旧回答晚到时不得覆盖新内容或通过旧finally解除新请求busy。

原有断言未降低；精确允许工具集合仅增加两个工具，澄清两轮脚本替身增加continue握手；离线Demo读取选中ID限定为单行，修复新system目录导致的替身解析回归。原有冲突、并行配对、恢复、批量取消、软删除、回滚及CRUD全部继续通过。

```sh
.venv/bin/python -m pytest tests/test_task_contexts.py -q
PATH="$PWD/.venv/bin:$PATH" RUN_LIVE_LLM=0 sh scripts/check.sh
```

**最终183通过、2跳过、0失败，覆盖率92%；Ruff、格式和JS语法通过。** 两项旧live pytest未执行，不能计为通过。独立审查修复后独立181通过2跳过且无新阻塞；最后新增2个边界测试后完整183通过2跳过。

## 真实模型与浏览器

真实模型 `deepseek/deepseek-v4.1-flash`，保持OpenRouter provider，进程覆盖模型、密钥从.env安全读取。独立合成库预置10/20 12–15占用，按五轮：提醒→理发→三点以后→恢复邮件→取消邮件。

实际**8次请求全部HTTP200，78611 tokens，API报告US$0.005698224**；上限10请求/$0.03，没有HTTP重试/Gemini/VPN/代理。直接答复轮首次漏做上下文选择，被context_required拒绝，模型随后continue正确hair ID并重查，无合并或写入。

真实“三点以后”最终模型选下界15:15（符合15点以后），不是15:00。提醒notes保留了较完整原句，恢复后与原提醒快照逐字段相同；不把它截成测试固定短句。对保存记录执行了原快照相等、hair任务ID不变、下界>=15、取消后hair仍挂起及HTTP断言，全部通过。真实试验没有选择或确认日程，活动记录始终为原合成占用。完整原参数、反馈、状态、context目录、usage/provider/request ID在Git忽略的 `data/context-sprint-20261017/live-report.json`。

浏览器另用localhost:8003独立cookie/合成库和**明确标注的脚本模型**验证：提醒能力说明→新理发有4个候选且邮件挂起→卡内“三点以后”→恢复邮件（hair挂起）→恢复hair（仍原范围/候选）→取消email但hair仍在→选择15–16仅出现确认卡（活动仍1）→显式确认活动2，理发准确15–16。控制台warn/error为空，截图context-isolation-slots.png、flexible-slot-confirmation.png。浏览器没有穷举网络/超时故障，相关证据来自自动化。

临时浏览器页和服务验收后关闭。主8002已用同一DeepSeek环境及原数据库重启/刷新，原日程保留；本轮没有对主日历发送创建或确认请求。

## 启动与边界

```sh
LLM_MODEL=deepseek/deepseek-v4.1-flash \
LLM_ENDPOINT=https://openrouter.ai/api/v1 \
APP_DATABASE=data/restore-fix-20261017/browser.sqlite3 \
  .venv/bin/python -m uvicorn scheduler.app:create_app --factory --host 127.0.0.1 --port 8002
```

在同一对话中发送提醒句，再发送理发句；观察旧任务挂起和理发候选。可在任务菜单恢复/取消某项，候选选择后再确认。自动化复现使用上面的pytest命令，无密钥要求。

边界：LLM仍决定自然语言任务边界，可能先调用错误工具；结构门禁阻止无边界续写并回送纠错，不能证明模型对所有语言都判断正确。尚未自动拆解已经污染的历史组合草稿，不静默猜测或删除原数据；可取消旧草稿、以新任务重述。最多50未完成context、目录最多70项，详情存储保留；事件草稿/提案/候选仍按各自15分钟期限，新范围需重查。提醒只保存未执行的请求，不产生通知。空档只依据本地日历占用与既有规则，不具备外部交通或通勤预测；需额外条件时要用户明确。应用仍按本地单用户单进程方式运行，不声称已验证多进程或公网多用户调度。

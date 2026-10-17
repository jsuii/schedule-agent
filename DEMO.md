# 演示指南：五个核心场景

## 启动与注意事项

使用Python3.12，已安装依赖后，在项目根目录：

```bash
source .venv/bin/activate
LLM_MODEL=deepseek/deepseek-v4.1-flash \
LLM_ENDPOINT=https://openrouter.ai/api/v1 \
APP_DATABASE=data/demo-new.sqlite3 \
python -m uvicorn scheduler.app:create_app --factory --host 127.0.0.1 --port 8002
```

打开[网页](http://127.0.0.1:8002/)，检查“当前模型”确实为DeepSeek。按README配置自己的 `.env` 密钥；环境变量只在新进程启动时生效；更换模型必须重启。文件名`demo-new.sqlite3`须为新的演示文件，或明确使用已有数据；不要删除普通开发数据库来重置演示。

以下“实际验证”来自历史阶段报告，本次提交前审查没有重复调用真实模型。以下每场按“初始状态”独立准备；可为每场选择不同的APP_DATABASE文件，或通过自然语言创建所需合成事件并逐个确认。新对话只重置上下文，不清空日历。任何工具提案都尚未写入，必须核对卡片并确认；下面只涉及合成日程。

无需API的固定脚本展示：`python -m scheduler.demo --database data/offline-new.sqlite3`，端口8001，明显标为模型替身，不作为真实LLM能力证据。

## 1. 清晰中文创建

- 初始状态：空合成数据库。
- 输入：“明天下午3点安排合成浏览器验收会议，持续1小时，地点演示室A。”
- 预期：必要时查询现有日程，再生成次日15–16点的待确认卡；确认前侧栏为空，确认后仅新增1条；地点演示室A。
- 实际验证：本轮真实浏览器（search_events→propose_create）、确认后读回及普通命令重启验证通过；截图见docs/screenshots/sprint-real-model.png。
- 限制：真实模型措辞/是否先查询可能变化；不要把直接显示提案当作已经保存。

## 2. 查询具体时间地点

- 初始状态：明天15–16点有“合成老王会议”，地点测试室A。
- 输入：“我明天有什么安排？”
- 预期：search_events使用当前时区日期范围，回复真实记录，不修改数据。
- 实际验证：本轮真实historical_update评估包含此查询，工具及结果已保存；历史独立只读协议测试也验证模型接收role=tool后生成中文回答。
- 限制：应用最终查询回复是对工具结果的事实渲染，不展示模型内部推理；模型自由文本不作为成功回执。

## 3. 多轮补全与修正

- 初始状态：空合成数据库。
- 输入1：“帮我安排明天下午的合成项目会议。”
- 预期1：问具体开始时间/时长，不擅自安排。
- 输入2：“三点开始，一个小时，地点测试室A。”
- 预期2：保留会议主题、明天日期，提出15–16点事件；确认后可查询。
- 可选修正：“改到下午4点，其他不变。”应提出新卡片，旧提案不能再确认。
- 实际验证：原两项live测试之一及本轮clarification场景真实通过；修正前文、旧提案失效、原时长保留由真实SQLite+脚本模型回归验证。可选修正文案未另行逐字真实模型测试。
- 限制：上下文只保留最近12轮；跨午夜续聊“明天”按新一轮真实当前时间解释，必要时明确日期。

## 4. 同名目标消歧

- 初始状态：明天15–16点和后天15–16点各有“合成老王会议”。
- 输入：“取消和合成老王的会议。”
- 预期：查询两条候选，追问选择；两条日程均不变。
- 点击明天的候选卡，再核对“待确认删除”，确认后仅删除这条，后天那条仍保留。
- 实际验证：本轮真实ambiguity评估3请求通过；用户选择后再次检索、选择不能泄漏到下一任务、候选集合变化和截断窗口均有离线回归。
- 限制：点击候选不是确认删除；自然语言“第二个”需模型重新缩小检索，演示建议直接点卡片。单项删除需明确目标；明确范围可走批量取消清单，一次确认后软删除。

## 5. 冲突与替代时间

- 初始状态：明天15–16点“合成老王会议”。
- 输入：“明天下午3点安排合成周报工作，持续1小时。”
- 预期：不写入重叠日程；校验冲突或查询空档后询问替代时间。不能在没有搜索时声称无空档。
- 补充：“那就改为明天下午4点开始，持续1小时。”
- 预期：提出16–17点周报；确认后原会议仍在，两个事件相邻不冲突。
- 实际验证：首轮真实失败已记录并以回归复现，修复后真实conflict_after_fix评估4请求通过。
- 限制：空档最多5个、15分钟步进，无工作日/节假日优先级；不会强制覆盖冲突。

## 一键离线验收与有预算的真实评估

```bash
PATH="$PWD/.venv/bin:$PATH" RUN_LIVE_LLM=0 sh scripts/check.sh
```

当前默认183通过、2个live测试跳过，覆盖率92%。两项live用例历史阶段曾单独实跑通过；本次审查未执行，默认跳过避免日常测试产生费用。

本轮评估记录`data/sprint-20261017/report.json`包含23次请求的synthetic工具参数、结果、用量和失败，受Git忽略。`scripts/evaluate_live.py`是针对本轮的有预算驱动，不会自动重跑已记录场景；`scripts/serve_validation.py`是同一create_app的观测封装。普通网页不使用这些测试替身或脚本路由，也不受评估账本的剩余额度限制。

再次付费评估前应明确新预算、保留原失败证据，并选取真正需要复验的场景。不要删除账本来绕过本轮上限。

## 新增：智能冲突工作流演示

本轮验收网页在 http://127.0.0.1:8002/ ，当前合成结果为宠物13–15:30、面试16–18。它使用真实DeepSeek，发送消息会产生费用；浏览器确认/取消/选择方案本身不请求LLM。

重做原始场景请用新的合成数据库，避免误改普通开发记录。以下命令在项目根目录执行（已有8002进程可保留，演示使用8003）：

```bash
source .venv/bin/activate
python - <<'PY'
from scheduler.storage import Store
from scripts.evaluate_conflicts import event
s = Store('data/conflict-demo-01.sqlite3', 'Asia/Shanghai')
if not s.calendar.search():
    s.calendar.create(event('合成宠物检查', '13:00', '17:00'))
PY
LLM_MODEL=deepseek/deepseek-v4.1-flash \
LLM_ENDPOINT=https://openrouter.ai/api/v1 \
APP_DATABASE=data/conflict-demo-01.sqlite3 \
python -m uvicorn scheduler.app:create_app --factory --host 127.0.0.1 --port 8003
```

1. 打开 http://127.0.0.1:8003/ ，输入“安排2026年10月19日16:00–18:00的合成面试”。
2. 核对宠物/面试卡片、16–17重叠、尚未修改状态、少量方案及假设。明确时间固定会减少可选方案数量。
3. 点击方案，核对统一前后对照；取消，面板仍保留且没有改日程。
4. 自定义框输入“面试保持16:00–18:00，合成宠物检查改为13:00–15:30，请将面试新增和宠物检查修改作为一次统一提案”，按Enter。
5. 核对两项后确认；看到原子提交回执，右侧宠物13–15:30、面试16–18。刷新/重启同数据库仍保留。
6. 暂停嵌套可另起新的合成库：先建19日11–16大扫除，再安排12–12:30寄快递。未知可暂停性应先澄清。明确双方在家、快递上门无需通勤、注意力可兼顾、允许重叠且大扫除可暂停恢复后，提交属性更新＋新增的统一提案。

A–H包括故障/过期/回滚可不花API费用复现：`RUN_LIVE_LLM=0 python -m pytest tests/test_conflicts.py -v`。模型有非确定性，真实测试失败轨迹及边界见[冲突验收](docs/CONFLICT_TEST_2026-10-17.md)。新对话只清上下文，不清日历；重做请改用新的demo数据库文件名。

## 当前推荐：一句话多事件与精简冲突面板

历史网页验收曾在8002确认两条合成记录，当前日历内容以实际数据库为准。重做请另用空合成数据库（不要清空普通开发库）：

```bash
cd schedule-agent
source .venv/bin/activate
LLM_MODEL=deepseek/deepseek-v4.1-flash \
LLM_ENDPOINT=https://openrouter.ai/api/v1 \
APP_DATABASE=data/ux-demo-new.sqlite3 \
python -m uvicorn scheduler.app:create_app --factory --host 127.0.0.1 --port 8003
```

1. 打开 http://127.0.0.1:8003/ ，输入原句：“今天11点至16点家里大扫除，12点去寄个快递，花15分钟。”
2. 核对两项独立时间和15分钟交集。默认只见一个推荐，“其他方案”和“查看调整详情”可展开；自定义输入始终可用。采用只生成提案，不写入；可以取消后继续。
3. 说“这两个可以并行，大扫除可暂停”，任务保留。若往返/注意力尚未知，应只问必要条件，不再问已给时长。
4. 补充“快递15分钟包含往返，注意力可兼顾且已核实；保持原时间和各自地点，快递地点留空”。
5. 核对统一提案：大扫除11–16（家里）、寄快递12–12:15（未填写地点），确认后两项同时出现。没有外部预约同步。

另试“今天9点到10点在甲室做合成整理，11点在乙室做合成登记”→补“登记15分钟，其他不变”，应保留第一项。模型验证与确定性验证分开记录于[UX验收](docs/UX_TEST_2026-10-17.md)。重复完整演示换新数据库名，原句用当前真实日期，不硬编码某一天。

## 当前推荐：批量取消、回收站与一键撤销

历史8002批量验收曾恢复3项，当前日历内容以实际数据库为准。要从干净初始状态重做，使用新的合成文件名：

```bash
cd schedule-agent
source .venv/bin/activate
python - <<'PY'
from datetime import datetime
from scheduler.storage import Store
from scripts.evaluate_trash import seed
s = Store('data/trash-demo-01.sqlite3', 'Asia/Shanghai')
if not s.calendar.search() and not s.calendar.trash():
    seed(s, datetime.now(s.calendar.tz).date().isoformat())
PY
LLM_MODEL=deepseek/deepseek-v4.1-flash \
LLM_ENDPOINT=https://openrouter.ai/api/v1 \
APP_DATABASE=data/trash-demo-01.sqlite3 \
python -m uvicorn scheduler.app:create_app --factory --host 127.0.0.1 --port 8003
```

1. 打开 http://127.0.0.1:8003/ ，输入“取消今天的所有安排”。一张清单显示3项与今天半开范围；确认前右侧仍3项。
2. 取消勾选羽毛球，等待服务端更新为2项；或自然语言“保留下午的合成羽毛球，其他都取消”。只需一次“确认取消所选2项”，活动1/回收站2。
3. 展开“回收站 · 保留7天”，核对原时间、删除/到期时间。恢复单项先生成提案再确认，活动2。
4. 点击“撤销最近删除”：只直接恢复原批次仍在回收站的剩余1项，活动3；不会重复恢复刚才已恢复的项或恢复其他批次。
5. 输入“取消本周所有安排”，新范围应重新包含3项（不继承旧排除）。确认后，从回收站选择“恢复这一删除批次”→核对3项→确认恢复。
6. 若恢复时被新安排占用，页面显示具体冲突，整批未恢复；可选择已验证的改期方案或明确子集后另行确认，也可保留回收站；不会偷偷改时间。

“查看回收站”可打开该面板；“恢复昨天删除的合成羽毛球”由模型先检索实际墓碑再提出恢复。“撤销刚才删除”的自然语言仍需确认；页面一键撤销是用户显式授权，不授予LLM执行权限。

范围约定：周一至下周一，下午12–18；清单显示实际范围。升级自动迁移，无手工SQL。7天到期后不可恢复，清理是显式有界函数，见[本轮验收](docs/TRASH_TEST_2026-10-17.md)。既有永久删除记录不能由迁移恢复。

不花模型费用验证：`RUN_LIVE_LLM=0 python -m pytest tests/test_trash.py -v`。真实评估入口`python -m scripts.evaluate_trash cancel_exclude_undo_after_schema|week|recovery_query|browser`已执行，不为演示报告重复运行或删账本绕预算。重做Demo用新的合成文件名，不清空普通开发库。

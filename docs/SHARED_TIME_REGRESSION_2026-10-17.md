# 多事件共享时间范围回归：定位、修复及验收

## 先查证据，后改代码

本轮先读取实际SQLite失败会话、历史模型tool_calls、工具结果、state、任务规范化与完整性校验，以及相关Git历史。没有先添加句子匹配或关键词路由。

用户原消息：`明天早晨9-10点去买菜、吃早餐`。实际持久化的模型参数为：

```json
{"intents":[{"intent_id":"grocery","version":null},{"intent_id":"breakfast","version":null}]}
```

**两项均没有event，title/start/end/duration均未输出。** grocery/breakfast只是模型生成的意图ID，不是事件标题。该应用没有一个先于LLM的独立中文时间解析器，因此不能声称时间已被解析成日期后再在拆分中丢失；已保存的证据表明丢失发生在模型输出工具参数之前。内部推理不可见，原轮完整system now没有持久化，不能补造模型当时的时间解析结果。

数据流证据：

| 层次 | 原失败实际结果 | 解释 |
|---|---|---|
| 用户消息 | 含“明天早晨9-10点”、买菜、吃早餐 | 原始文本保存在历史中 |
| 模型stage_task参数 | 只有两个intent_id和null version | 未输出任何可规范化的标题/时间 |
| DraftIntent校验 | event可省略 | 把空提取当作有效草稿 |
| stage合并 | `base | raw.get('event', {})` → 两个event `{}` | 没有父时间范围可继承，没有字段被代码删掉 |
| 完整性校验 | 三字段皆缺 | 正确读到空草稿，但错误地将模型提取遗漏转成用户追问 |
| 追问 | intent_id代替title，列出全部缺失 | 程序无法从空event恢复已给内容 |
| 状态 | 两个空event持久化 | 后续轮会继续携带有缺陷草稿 |

额外协议缺口：原TaskDraft没有父级/共享范围字段，只有每项event和sequential/parallel/interrupt关系；合法的“范围已知、分配未知”无法准确表达。duration_minutes也没有草稿字段，原来依赖模型自行计算end。

Git证据：`042a1c8` 引入tasks.py和可省略event的DraftIntent，此契约在本次修改前仍保持；最近回收站/恢复修复没有改tasks.py。没有证据支持“恢复代码删掉了已解析标题/时间”，也没有把LLM非确定性伪装成固定代码映射回归。

## 最小结构修复

- `scheduler/tools.py`：DraftIntent.event改为必填的非空Patch；不允许只交ID或event=null。模型得到针对stage_task的结构错误反馈，可在现有Agent循环中修正，而不是让用户重新给已给信息。部分事件仍可只保存已知title/start等。只改relations仍可intents=[]。
- TaskDraft新增 `time_windows[{window_id,intent_ids,start,end}]`，保存组共享范围；不等于任何子事件的已确认区间，更不等于并行许可。
- DraftIntent新增明确时长 `duration_minutes`，不进入日历EventInput；由 `start + duration_minutes` 推导end，保留元数据。显式end与duration矛盾时拒绝，不静默覆盖。
- `scheduler/tasks.py`：窗口按window_id跨轮合并，校验引用ID/重复ID/有效范围，并转换为日历时区ISO。子event已知字段按intent_id保留，不复制其他事件的地点、时长或优先级。
- 未分配窗口生成一次针对范围及时间分配的澄清；不为每项重复询问已知标题/开始/结束，已知单项边界也在澄清中展示。完整窗口约束同时进入草稿检查、冲突方案选择和确认事务的validate_task；不能绕过范围移动事件。
- duration推导完成后，再统一检查之前草稿的硬约束，避免更新start时先拿旧end误报；固定时间仍不能通过改duration绕过。
- 并行完整区间若缺少往返/注意力核实，会同时要求核对各项起止、是否占用完整区间；不授予重叠许可、不创建可执行提案。
- `scheduler/agent.py`：增加通用字段使用说明以配合真实结构，不添加中文句子规则、单独意图分类器或隐藏时间分配。原工具循环与LLM边界不改。

保留现有冲突、配对权限、提案确认、事务、回滚、版本和任务revision逻辑；无数据库迁移，无外部日历。旧任务没有time_windows时仍按原契约读取。已有失败空草稿不会从ID猜标题；过期草稿仍需新建对话。

## 失败先行与自动回归

新增 `tests/test_shared_time.py`。初次8个用例 **7失败、1通过**：原失败参数被错误接收；共享窗口/时长为不支持字段；独立开始时间原本正常。随后增加持续时间更新回归：原先派生前约束检查误用旧end，观察失败后调整校验顺序。

最终新增13项测试，覆盖：原始空参数拒绝与同轮模型修正、要求的五条输入对应结构、多轮/重启/确认、范围越界阻止、固定约束、duration重新计算、真实并行参数回放、不一致时长和无效引用不污染状态。所有单元/集成用例使用脚本模型和真实SQLite，**不是中文自然语言模型解析测试**；脚本输出结构由fixture明确提供。

```sh
.venv/bin/python -m pytest tests/test_shared_time.py tests/test_orchestration.py -q
PATH="$PWD/.venv/bin:$PATH" RUN_LIVE_LLM=0 sh scripts/check.sh
```

最终完整门禁 **159 passed、2 skipped、0 failed，覆盖率92%**；Ruff检查、格式检查、JS语法通过。两个旧live pytest未执行，不能计为通过。独立审查曾发现并复核duration更新问题，修复后的中间完整门禁157通过2跳过；最后增加2项边界后主流程159通过2跳过。

## 五句真实模型抽查

模型：`deepseek/deepseek-v4.1-flash`，OpenRouter，进程环境覆盖，密钥从.env安全读取，未修改.env。固定每个场景独立会话和合成库，不读取用户日历，不确认或执行写入。基准时间 **2026-10-17T10:13:53.606134+08:00**，明天即 **2026-10-18，Asia/Shanghai**。

| 实际用户输入 | 真实提取与程序结果 |
|---|---|
| 明天早晨9-10点去买菜、吃早餐 | title买菜/吃早餐，time_windows 10月18日09–10，无子start/end；一次时间分配澄清，不问标题 |
| 明天9点买菜，10点吃早餐 | title均保留，start分别09/10，end都未猜；只问结束或持续时间 |
| 明天9点到10点先买菜再吃早餐 | title与09–10共享范围保留，sequential confirmed=true；问分配/分界，不均分 |
| 明天9点到10点买菜，同时吃早餐 | 模型提出每项09–10的暂定区间，parallel confirmed=true；注意力/往返未核实，canonical overlap_allowed/coexistence_verified均false，无提案无写入 |
| 明天11点至16点家里大扫除，12点去寄快递，花15分钟 | 大扫除11–16地点家里，快递12–12:15、duration15、地点空；转入原有冲突/暂停澄清，不问时长 |

实际全部5次请求HTTP200，**37479 tokens，API报告US$0.011012875**。上限10请求/$0.025，实际各场景一次请求后stage_task返回澄清，停止新增调用，无自动HTTP重试、Gemini请求或代理绕限制。完整原参数、tool结果、任务状态和usage位于Git忽略的 `data/shared-time-20261017/report.json`，分别命名shared/separate/sequential/parallel/nested。对保存的五组结果又执行了离线结构断言，全部通过。

真实记录的并行句没有使用time_windows，而是给出两项完整区间：这仍是待用户核对的解释，不可宣称用户已确认两项各持续一小时。真实检查后又补充了确定性的并行澄清文案，明确核对完整区间，并以捕获参数做离线回放测试；**这一文案细化没有再调用真实模型**。不能把这5次抽查扩大成自然语言解析永不出错或五次真实写入通过。

请求证据（generation ID作为服务返回的请求标识）：

| 场景 | Provider | ID |
|---|---|---|
| shared | Relace | gen-1792203235-FQEF9nCmlRGczZg77AgE |
| separate | Phala | gen-1792203237-X97JQD8a3IlWKbEvnWOa |
| sequential | DigitalOcean | gen-1792203240-cxdOMqYIuPTkFTS0GdHw |
| parallel | Alibaba | gen-1792203242-sjCR9yGSJc08NGSBDtbT |
| nested | Together | gen-1792203248-yCtFB5B5wZQZMzJGQ3ly |

## 边界与运行

没有独立规则解析器；LLM仍可能漏提非空字段、误解时间分配或把共享范围表达成每项完整区间。结构校验能阻止ID空壳及不合法边界，不能在没有原文语义解析的情况下证明每个模型字段都来自用户。并行条件和最终人工确认继续把关，缺失分配不自动执行。

支持最多8事件/8窗口，任务保留15分钟；同一window_id可更新范围，未实现删除窗口的专门操作。跨度/时间格式按现有日历校验；没有在本轮穷举跨时区、多组重叠窗口和所有自然语言表达。真实小样本未覆盖后续确认写入，确定性测试覆盖重启、统一确认与现有原子回滚。

8002保持DeepSeek及原数据库，重启已加载新代码；本轮不自动重放用户创建请求。旧空草稿过期时可点击“新对话”再试原输入。启动沿用README的显式LLM_MODEL/LLM_ENDPOINT覆盖命令，避免落回Gemini。

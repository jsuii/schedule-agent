# 留白 · 中文日程 Agent

一个本地运行的网页日程助手：LLM 自主选择工具、多步检索与澄清；应用负责校验、冲突判断、安全确认和 SQLite 持久化。

**当前状态：智能冲突工作流已接入网页。离线96通过、2个live默认跳过；本轮DeepSeek共25次请求、API报告US$0.0218568444，包含3个未通过的中间验收场景。** 已验证自定义多事件调整、暂停嵌套、固定冲突方案及原子确认；失败原因、修复、用量审计限制见[冲突验收](docs/CONFLICT_TEST_2026-10-17.md)。Gemini默认配置保留，未重试；未同步外部预约。

## 已实现

- 中文对话网页，支持手机宽度、对话历史、日程列表、日期筛选、候选选择、工具执行摘要。
- 真正的 `model → tool → observation → model` 循环；缺失信息和冲突会回到模型继续决策。
- 真实 SQLite 新建、查询、修改、删除；会话、提案和幂等回执跨重启保存。
- 标题/地点/备注检索、中文短词模糊召回、日期区间过滤、同名候选消歧。
- 精确冲突事实、稳定ID的多方案与自定义输入；显式核实的可暂停嵌套；批量调整单次确认、原子回滚。
- 所有写操作先展示完整提案，由用户按钮确认；确认时再校验冲突和版本，写后读回。
- 模型自由文本不能当作成功回执；澄清由模型选择字段、程序生成中文问题，避免夹带虚假成功声明。
- 独立 provider/interface，可更换 endpoint/model/key，无需修改 Agent 或业务服务。

## 环境与安装

需要 **Python 3.12+**。测试另需 Node.js（仅 JavaScript 语法检查，无 npm 依赖）。本机系统 `python3` 是 3.9，不适用；可使用已有 Conda 环境创建隔离环境：

```bash
cd /Users/jiayusui/Developer/projects/schedule-agent
conda run -n llm-dev python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]' -c requirements.lock
```

其他机器可用 `python3.12 -m venv .venv`，后续相同。`requirements.lock` 固定完整依赖，`pyproject.toml` 声明直接依赖。应用运行不依赖 Conda、AgentScope、Google Calendar 或 Docker。静态网页随 Python 包打包，无前端构建步骤。

## 无 API key 的本地演示

```bash
source .venv/bin/activate
python -m scheduler.demo --database data/demo-01.sqlite3 --port 8001
```

打开 [离线演示](http://127.0.0.1:8001)。点击“演示下一步”依次展示：

1. 缺失具体时间和时长 → 追问。
2. 补充信息 → 待确认创建 → 点击确认，右侧出现真实记录。
3. 查询明天的安排 → 从数据库返回。
4. 修改地点 → 核对前后内容 → 确认修改。
5. 同一时间写周报 → 检测冲突 → 展示可用时段，不写入周报。
6. 创建后天的同名会议 → 确认。
7. 取消同名会议 → 展示两条候选 → 选择卡片 → 确认删除，仅删除选中的一条。

普通输入不匹配演示脚本时会明确提示；演示不是模型效果测评。重复完整演示请换一个新的 `--database` 文件名，避免与之前的真实演示记录冲突。`新对话`只清空当前会话上下文，不清空日历。默认普通数据库为 `data/calendar.sqlite3`，与演示分开。

## 普通模式（当前可用模型）

`.env`仍保留Gemini默认，当前直接使用该模型会遇到403。通过进程环境覆盖使用已验证的DeepSeek，不需要修改源码或覆盖密钥：

```bash
LLM_MODEL=deepseek/deepseek-v4.1-flash \
LLM_ENDPOINT=https://openrouter.ai/api/v1 \
python -m scheduler
```

打开 [本地应用](http://127.0.0.1:8000)。未配置模型时网页仍能启动、查看日程、显示配置提示；发送自然语言会明确返回“尚未配置”，不会创建虚假数据。

自定义本地端口：

```bash
LLM_MODEL=deepseek/deepseek-v4.1-flash \
LLM_ENDPOINT=https://openrouter.ai/api/v1 \
python -m uvicorn scheduler.app:create_app --factory --host 127.0.0.1 --port 8010
```

只使用单个进程。`Ctrl+C` 停止服务，事件和会话保留。数据库首次启动自动初始化，schema 版本为 1。

## 后续真实模型配置

OpenRouter密钥已保存在本地被忽略的 `.env` 中。环境变量优先级已验证，运行进程的模型ID可在网页及 `/api/health` 确认；它只代表实际加载配置，不代表每次模型请求必然成功。其他环境配置时：

```bash
cp -n .env.example .env
# 在本地编辑 .env，不要把密钥粘贴进聊天、截图或提交到 Git。
```

| 变量 | 含义 |
|---|---|
| `LLM_ENDPOINT` | 完整 `/v1/chat/completions` 地址，或以 `/v1` 结尾的 base URL（自动追加 `/chat/completions`） |
| `LLM_MODEL` | provider 实际可用、支持工具调用的模型 ID |
| `LLM_API_KEY` | 该端点对应的密钥；无认证本地服务可以为空 |
| `APP_TIMEZONE` | IANA 时区，默认 `Asia/Shanghai` |
| `APP_DATABASE` | SQLite 文件，默认 `data/calendar.sqlite3` |
| `APP_CONFIG` | 可选 JSON 配置文件路径 |

配置文件方式：复制 `config.example.json` 为被忽略的 `config.local.json`，填写 `endpoint`、`model`、`api_key`。`api_key` 可写 `${LLM_API_KEY}` 从环境获取。优先级：系统环境 > `.env` > JSON > 默认值。配置修改后重启服务。

兼容性要求：支持非流式 Chat Completions、`tools`/`tool_choice:auto`、JSON Schema 参数定义（包括 `$defs/$ref`）、assistant.function `tool_calls`、字符串 JSON arguments，以及 `role:tool`/`tool_call_id` 多轮回传。程序没有假设 Responses API、Anthropic API 或仅文本模型兼容；不兼容会明确报错，无自动“伪工具”降级。远程 endpoint 要求 HTTPS；仅 loopback 允许 HTTP；不会跟随重定向发送密钥。

之后可运行真实模型验收（只发送合成测试内容，数据库为临时独立文件；会产生 provider 的正常调用费用）：

```bash
LLM_MODEL=deepseek/deepseek-v4.1-flash \
LLM_ENDPOINT=https://openrouter.ai/api/v1 \
RUN_LIVE_LLM=1 python -m pytest -m live -v
```

本轮这两项检查已通过，覆盖中文相对日期、多轮补全、检索后修改及真实工具协议。未来重新运行仍会产生费用，应在普通网页试跑以上演示场景，并记录具体模型/日期/效果。离线结果不能证明某个真实模型的中文理解质量或 tool-calling 兼容性。

## 测试与检查

```bash
python -m pytest -q
sh scripts/check.sh
```

当前结果及覆盖范围见 [TESTING.md](docs/TESTING.md)。本轮默认96通过2跳过；另以独立评估器执行真实冲突场景，未重跑这两项旧live。默认所有模型测试使用脚本替身或 HTTP mock，真实模型两项默认跳过；CRUD、SQLite、HTTP 接口和事务不是 mock。

## 架构与文件

```text
scheduler/
  provider.py       Model Protocol + OpenAI 兼容 HTTP 实现
  agent.py          有界自主工具循环、上下文、事实回复
  tools.py          参数 schema、候选门控、写入提案、澄清字段
  conflicts.py      冲突事实、候选方案、原请求硬约束与快照校验
  calendar.py       时区、检索、冲突、空档、事务 CRUD 与读回
  storage.py        持久化会话、提案、确认、幂等请求
  app.py            FastAPI、本地会话与安全边界
  demo.py           显式离线脚本模型替身（独立入口）
  static/           原生 HTML / CSS / JavaScript
```

完整设计见 [DESIGN.md](DESIGN.md)、[需求追踪](docs/REQUIREMENTS.md)、[产品设计](docs/PRODUCT_DESIGN.md)、[架构](docs/ARCHITECTURE.md)、[仓库评估](docs/RESEARCH.md)。原始题目 `test.md` 未修改。

## 常见问题与限制

- `python` 版本太旧：使用 Python 3.12+，不要用本机 `/usr/bin/python3`。
- 端口占用：换端口；浏览器地址也要相应修改。
- 401/403：检查密钥和模型权限；400：检查 Chat Completions tools 支持；404：检查 endpoint/model；429：稍后重试。不会把 provider 原始响应或密钥返回前端。
- 超时：单次 HTTP 默认30秒、整轮90秒、最多8次模型调用；同一工具连续3次同类失败会提前停止；不自动重试 HTTP 请求，避免费用与重复操作。工具参数错误返回字段路径和类型供模型修正，不回显字段值。
- 提案失效：15分钟有效；新消息使旧提案失效。日程版本/冲突改变时确认会拒绝，需要重新查询和提案。
- 同名事件：先缩小查询或选候选卡片，再确认。选择只在下一模型回合内有效，重查候选变化会要求重新确认。批量取消需要逐条核对，不支持一次清空。
- 模糊检索是字符相似度召回，不是 embedding；短词可能多召回。模型可调整关键词，写入仍需明确确认。
- 单个事件最长7天；空档搜索最多31天、按15分钟步进返回最多5个；没有工作日/节假日规则。查询工具最多50条，截断有标记；侧栏最多500条。
- 日期必须有具体时间；没有默认推定“一小时”；多轮相对日期以每轮当前时间为准。无偏移本地 DST 重复/不存在时间会拒绝，需明确偏移。
- 最近12个完整轮次保留在模型上下文中；不支持无限历史。会话、请求、过期提案没有自动磁盘清理任务，长期使用需另加保留策略。
- 本地单用户，共享同一份本地日历，随机 cookie 隔离的是对话和提案，**不是多租户数据权限**。仅绑定 loopback，不适合直接公网部署；多 worker 也不在支持范围。
- 外部模型配置后，当前会话与检索到的日程会发送给该 endpoint；请只配置可信服务并使用合适的数据。默认不联网推理、不读取其他日历账号。
- `.env`、本地配置、SQLite、缓存均被忽略。前端以 textContent 渲染，SQL 参数化，确认端点防跨站并检查会话，密钥不写日志。
- 没有替你创建/推送 GitHub 共享仓库；发布、推送和面试官权限开放待单独授权。本轮历史修改重放通过，但旧失败缺乏原始参数，根因无法追溯；不把小样本通过宣称为任意输入均可靠，也不把本地交付等同GitHub外部验收完成。

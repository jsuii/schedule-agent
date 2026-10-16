# 仓库优先评估

2026-10-17：使用 GitHub connector fetch 读取公开仓库元数据，浏览仓库网页，再 shallow clone 至 /tmp/schedule-research 检查真实实现。未执行上游代码或复制代码。

| 候选 / 检查版本 | 可复用能力（源码证据） | 缺口 / 风险 | 预计适配 | 许可 / 结论 |
|---|---|---|---|---|
| [craigk5n/scheduling-agent](https://github.com/craigk5n/scheduling-agent) cca5356 | graph.py 校验/确认/读回；calendar.py MCP；tests/test_graph.py 等 | 固定 parse→gather→execute 图；WebCalendar/PHP/MCP 是额外部署；核心需替换为自主工具循环 | 1–2天（估计） | MIT © Craig Knudsen；不采用 |
| [evanlambb/Calendar-Agent](https://github.com/evanlambb/Calendar-Agent) 070701b | backend/agent.py 的 bind_tools + ToolNode 循环；tools.py 的 Google CRUD；tests 三类测试 | Google OAuth、Gemini 耦合；InMemorySaver；冲突主要靠 prompt；修改工具不完整；CLI import 有交互副作用 | 1–2天（估计） | MIT © Evan Lamb；不采用 |
| [anthroos/open-schedule-agent](https://github.com/anthroos/open-schedule-agent) 7dffc9b | core/engine.py 5步工具循环；llm/openai.py provider；SQLite conversation/booking；channels/web.py；多项测试 | Google 是事件后端；SQLite 是预约/会话；多渠道/owner/guest/通知平台范围过大；OpenAIProvider 使用同步 client；所需本地自由 CRUD 要重做 | 1–2天（估计） | MIT © Ivan Pasichnyk / WeLabelData；最接近但不采用 |

能力判断来自源码静态检查；上游测试未运行、真实模型和日历账号行为未验证。最近提交日期分别 2026-07-15、2025-07-19、2026-02-26；不以 star 数或维护时间推断可靠性。没有为凑数量继续搜索，因为三个候选已提供足够技术比较。

## 框架对比与决定

- AgentScope：本机 llm-dev 实测版本 2.0.10；`agentscope.agent` 导出 Agent / ReActConfig，旧的 ReActAgent import 失败。检查安装源码及[官方2.x FAQ](https://agentscope.io/blog/agentscope-faq/)确认模型适配、工具及权限能力。可用，但仍需自己实现事务、确认、检索和网页，额外适配缺少收益。未进行真实 provider 联调，不声称不兼容。
- Dify：检查[官方部署文档](https://docs.dify.ai/en/self-host/deploy/quick-start/docker-compose)，依赖多服务 Docker Compose，源码与日历工具仍需自建；不利于单进程、本地可复现演示。未部署验证。
- 选择轻量自定义循环：HTTPX 调用 OpenAI 兼容 Chat Completions tools，Pydantic 生成工具 JSON schema，FastAPI + 原生 HTML/CSS/JS + SQLite。LLM 自主决定多步路径，业务边界确定性实现。无需额外日历账号；最少部署组件。

新增代码均为本项目编写；无修改上游或复制上游代码。所列估时是工程判断，不是实测工时。无需引入三份上游 LICENSE；链接保留用于研究归因。

# 当前验证：智能冲突轮

完整门禁 **96通过、2跳过、0失败，覆盖率94%**；24项新增冲突测试没有调用真实LLM。真实DeepSeek25请求、138296 tokens、US$0.0218568444，含失败轨迹；这两项默认跳过的旧live本轮没有重跑。详情、A–H映射、浏览器范围、用量账本审计限制见[冲突验收](CONFLICT_TEST_2026-10-17.md)。

```bash
PATH="$PWD/.venv/bin:$PATH" RUN_LIVE_LLM=0 sh scripts/check.sh
```

以下为上一轮可靠性测试历史（72通过等数字不代表本轮）：

# 测试与验收记录

最近验证：2026-10-17，Asia/Shanghai。环境macOS arm64、Python3.12.15、Node26.12.0。当前应用代码、mock测试与真实模型证据分开记录，历史失败报告保留。

## 本轮实际结果

| 检查 | 命令/方式 | 实际结果 |
|---|---|---|
| 默认完整离线套件 | `RUN_LIVE_LLM=0 .venv/bin/python -m pytest -q` | **72通过、2跳过、0失败** |
| 干净venv完整门禁 | `PATH="/tmp/schedule-clean-venv/bin:$PATH" sh scripts/check.sh` | **72通过、2跳过，覆盖率94%**；Ruff lint/format、JS语法通过 |
| 两项原有真实模型测试 | DeepSeek环境覆盖，RUN_LIVE_LLM=1，传输层共用本轮预算后执行test_live.py | **2通过**，实际5次请求，未修改断言 |
| 独立审查 | 独立阅读候选状态/保存点/模型标识/评估脚本及重跑离线套件 | 无重要发现；72通过2跳过 |
| 真实网页 | 8002使用DeepSeek；隔离数据库；浏览器提交/确认/重启 | 创建与确认读回通过，重启保留会话/事件；控制台warn/error为空 |

默认两项skip是主动防止日常测试产生API费用：test_live_chinese_multiturn_and_create、test_live_fuzzy_reference_uses_tools_and_proposes_correct_change。它们是重要验收，本轮已经单独真实运行通过；不是“跳过所以默认算通过”。

干净环境仍有一条Starlette 1.8.0弃用提示，建议未来TestClient迁移httpx2；当前HTTPX0.28.1测试全部通过，未屏蔽warning，未为此引入新依赖。

## 测试层次与覆盖

| 层次/文件 | 证据 |
|---|---|
| test_calendar.py | 实际CRUD、UTC/时区、半开相邻、并发冲突、SQLite重启、模糊查询、日期验证、DST gap/fold、SQL参数化 |
| test_agent.py / test_regressions.py | 脚本模型多步observation、补全、目标观测、歧义、超时/格式、循环、虚假成功拦截、旧提案、移时间保留时长、空档实际显示 |
| test_web.py / test_demo.py | 真实FastAPI/SQLite的确认、重复请求/确认、跨会话/Origin/Host、重启、完整离线CRUD/消歧/冲突流程 |
| test_sprint.py | 本轮问题：选择后重查、选择不泄漏、新候选使提案失效、截断列表明确选择、核验失败新增/修改/删除回滚；跨午夜/多冲突/前文修正及安全模型ID |
| test_provider.py | HTTP MockTransport检查真实请求体、配置优先级、SecretStr、401/400/429/500、超时和格式错误；不调用网络 |
| test_live_support.py | 评估计数跨驱动实例持久化、预算前置记账、403阻断继续请求、凭据和私有reasoning不进入日志 |
| test_live.py | 原有两个中文真实模型用例；默认skip，本轮单独DeepSeek运行2通过 |

默认测试仅替换模型边界，Calendar/SQLite/工具/API都是真实代码。新增失败回归先运行观察失败，再修复并重跑相关及完整套件。没有增加规则自然语言解析器，没有把mock成功当成真实模型决策证据。

## 本轮真实DeepSeek评估

所有请求model为`deepseek/deepseek-v4.1-flash`，endpoint归一化到OpenRouter Chat Completions。环境覆盖保留`.env`的Gemini默认；只用合成数据库，不读取真实个人日程/外部日历。

| 场景 | 观察到的工具路径 | 请求 | 结果 |
|---|---|---:|---|
| 历史创建→查询→修改重放 | propose_create；search_events→结束；propose_update | 4 | 通过，无法据此追溯旧记录缺失的原根因 |
| 同名消歧与删除 | search_events→ask_user；点选后propose_delete | 3 | 通过，仅删除选中记录 |
| 多轮缺时刻/时长 | search_events→ask_user；补全后propose_create | 3 | 通过，保留日期/主题 |
| 冲突初测 | search_events→ask_user(alternative) | 2 | **失败**：未查空档却声称无空档，已保留证据 |
| 冲突修复后一次复测 | search_events→find_slots→ask_user；选16点后propose_create | 4 | 通过，15点原事件保留 |
| 原有pytest live两项 | 原始断言、实际provider与SQLite | 5 | 2通过 |
| 真实浏览器创建 | search_events→propose_create，用户按钮确认 | 2 | 通过 |

合计**23次请求，输入48,386 tokens、输出3,188 tokens、总计51,574 tokens**；API报告费用合计**US$0.0096942304**。包含失败场景消耗，未删去失败来美化结果。评估上限24请求、按API费用阈值US$0.05停止、每请求最多2048输出token；未刻意用完剩余1请求。API费用是响应报告值，账单以账户为准。

私有合成记录位于Git忽略的`data/sprint-20261017/report.json`，保留工具ID/JSON参数/实际observation/最终状态/usage，排除key和私有思维链。HTTP失败只存状态码、脱敏错误、provider、request ID；无无限重试或自动切换模型。测试驱动按单进程顺序执行，预算账本不是多进程生产计费系统。

## 重要根因与回归

1. **候选选择状态**：search无条件替换state丢掉selected_id；selected_id又可能长期留在session。现在选择只供下一回合消费，同回合重查保留同ID/版本，下一任务不继承。
2. **候选集合变化**：只验证目标version无法发现后来新增的同名事件。现在存原查询并在提案前重查同一截断窗口；变化则要求重新查找/消歧。
3. **失败写入提交**：确认外层事务内捕获verification_failed后仍commit。现在日历操作包含保存点，失败回滚写入，持久化失败回执；新增/修改/删除故障注入验证原数据恢复。
4. **未知与空结果混淆**：未查询空档的状态不等于查询返回空。None/[]分开，缺证据时返回missing_observation给模型继续决定工具，不编造“无空档”。

普通assistant.content的假成功刷新后不出现：原逻辑已通过测试，不为此改写。历史修改旧失败本轮一次重放通过，未取得原参数，保留“旧根因不可追溯”的限制。

## 界面、启动和持久化

浏览器显示后端实际model ID，可核对运行进程配置。截图见[sprint-real-model.png](screenshots/sprint-real-model.png)。主流程确认前数据库为空，确认后1条；使用普通README uvicorn工厂命令重启，事件、聊天回执仍在，模型仍为DeepSeek。没有在浏览器逐项手动重做全部CRUD，其余通过HTTP自动化及真实Agent评估，不混淆验证方式。

手动停止观测入口时曾有预期的KeyboardInterrupt清理栈，未影响业务请求；入口现只静默处理显式KeyboardInterrupt，不吞掉其他异常。普通服务由uvicorn管理关闭。

## 可复现命令与已知限制

```bash
source .venv/bin/activate
sh scripts/check.sh

# 以下会产生模型费用；本轮已执行，不需要为报告重复运行。
LLM_MODEL=deepseek/deepseek-v4.1-flash \
LLM_ENDPOINT=https://openrouter.ai/api/v1 \
RUN_LIVE_LLM=1 python -m pytest -m live -v
```

`scripts/evaluate_live.py`和`scripts/serve_validation.py`提供本轮合成数据观测路径，保留原账本，不通过删除记录绕过预算。五场demo初始状态及限制见[DEMO.md](../DEMO.md)。

真实模型仍有非确定性，小样本不保证任意输入；“第二个”等口语复杂消歧未做穷举，推荐卡片确认。Gemini403保持为外部阻塞，不再重试。公网/多租户、周期、外部同步不在范围；跨浏览器与读屏器全覆盖未做。没有推送GitHub或操作真实日历。

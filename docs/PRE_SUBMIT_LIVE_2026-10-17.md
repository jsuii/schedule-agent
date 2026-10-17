# 提交前真实模型小规模复验 · 2026-10-17

实际模型ID：`deepseek/deepseek-v4.1-flash`。端点为OpenRouter Chat Completions，以进程环境覆盖；API key只从本地 `.env` 安全读取，没有改写 `.env`、provider、Agent、mock测试或用户日历。未请求Gemini，未使用VPN或代理，未推送GitHub。

## 结果

| 场景 | 实际结果 | 真实HTTP200数 |
|---|---|---:|
| 最小中性推理 | 2加3回答5，通过认证、模型响应检查 | 1 |
| 原生工具闭环 | 模型调用只读合成工具read_synthetic_count，程序返回role:tool及匹配tool_call_id，下一次模型响应以中文准确回答计数7；无后续工具需求 | 2 |
| 明天早晨9–10点去买菜、吃早餐 | task_context → stage_task；两个标题和次日09–10共享范围正确保留，提出时间分配澄清，未均分/写入 | 2 |
| 切换到独立理发安排 | task_context → schedule_flexible；新scope获得候选，旧scope挂起，不继承旧草稿字段 | 2 |
| 恢复买菜和早餐 | task_context → ask_user；回到原scope，完整task快照逐字段等于挂起前，理发scope挂起 | 2 |
| 明确早餐09–09:15、买菜09:15–10后生成提案 | **未完成真实验证**：调用前预算门禁已用完，零新增网络请求；没有提案，不算通过 | 0 |

总计10次尝试：首次受限执行环境ConnectError，未收到HTTP响应；申请直接网络权限后9次HTTP200。首次失败仍计入总上限，未重置或删除账本。连接失败无状态码、provider、request ID可记录；无401/403或其他HTTP失败，未触发访问限制绕过或重复请求。最后一次Agent本地返回预算错误，而非模型解析错误；按授权上限停止，未增加额度。

API实际返回usage合计：**55,532 tokens，US$0.0044401388**。连接失败无usage，不能据此声称该请求有已确认的计费记录；上述费用为9个成功响应的API报告值，不是账户账单核对。费用门限US$0.03、尝试上限10均未扩大。

OpenRouter自动路由的provider分别为Relace、Decart、InferenceNet、DeepInfra、AtlasCloud；全部成功响应model均为上述DeepSeek ID，没有手动切换模型。合成只读工具闭环request IDs：`gen-1792210123-nskm0ak4ktzLdts9i373`、`gen-1792210125-bkqE18KuklUonkhRKUWy`。

## 证据与边界

原始脱敏合成证据、usage、provider与request IDs保存在被忽略的 `data/pre-submit-live-20261017-120825-eb866d/report.json`，独立合成SQLite位于同目录；均不提交Git。未输出或记录密钥、私有reasoning；仅记录合成请求及工具结果。原日历未读写；合成库也无新增事件，所有Agent回合后均断言活动事件为空。

本次证明当前账户的基础推理及真实工具协议可用，并复验多事件字段保留、任务切换和恢复。没有证明本轮澄清后提案、用户确认写入、浏览器真实模型完整流程，也未执行两个旧live pytest。此前离线183项含提案/确认/事务测试；不能把它们算为本次真实模型通过。

复验后完整离线门禁再次运行：183通过、2个live跳过、覆盖率92%；Ruff lint/format及JS语法通过。本次没有发现需更改产品实现的提交阻塞问题；后续若要补齐真实提案环节，需要另行授权请求额度，不能从本报告推断已验收。

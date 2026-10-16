# 备选模型与环境变量切换验证（2026-10-17）

## 配置验证

保持当前Agent、provider和所有mock测试不变。Settings.load使用“系统环境 > .env > JSON > 默认值”的优先级，LLM_MODEL、LLM_ENDPOINT可在启动进程时覆盖；密钥继续从根目录.env读取。

本次通过以下环境覆盖运行临时验证驱动，没有改变.env的Gemini默认配置，没有重启当前网页：

```bash
LLM_MODEL=deepseek/deepseek-v4.1-flash LLM_ENDPOINT=https://openrouter.ai/api/v1 .venv/bin/python <验证驱动>
```

归一化地址为 `https://openrouter.ai/api/v1/chat/completions`。实际加载模型与响应model均为 `deepseek/deepseek-v4.1-flash`；断言检查通过。`git diff --quiet -- scheduler tests`通过，证明源码与mock测试没有改变。

## 模型选择与结果

优先选择此前账户已成功使用过的DeepSeek候选，再用本轮实际调用确认当前可访问性；没有枚举或轮流尝试大量模型，没有请求Gemini。

- 中性基础推理：成功，回复“你好！”。
- 真实tool calling：成功。使用项目CompatibleModel、search_events的真实schema和Tools.execute，读取独立SQLite合成会议。
- 模型自主生成search_events调用和JSON参数；工具真实执行后以role=tool和原tool_call_id回传；下一次模型请求产生中文最终回答。
- 最终回答：“中性合成测试会议”将于 2026年10月18日 15:00–16:00 在测试室A 举行。
- 断言回答包含15点和测试室A；日程读回与原始fixture一致，没有写入或删除普通用户日历。
- 本轮不需要第二个不同的工具调用；未声称测试过多工具规划、真实更新/删除、冲突消歧或原有修改失败修复。

## 请求证据

| 请求 | HTTP | Provider | 响应ID（未提供独立header request ID时使用generation ID） |
|---|---|---|---|
| 1 | 200 | CoreWeave | `gen-1792179954-vDameT4Wh8tEZmjKfDTt` |
| 2 | 200 | Relace | `gen-1792179956-tSeD2wyRs1TjKi9IeUAE` |
| 3 | 200 | Relace | `gen-1792179957-FGymbiQxgHoz2gk0yXkE` |

全部3次请求成功，没有失败请求；驱动对失败仅记录状态码、脱敏错误、provider和request ID，遇到失败立即停止，不自动重试、不更换网络绕过。禁用HTTP重试，不使用自定义VPN、代理、地区或路由策略。

API报告用量：输入 1167 tokens、输出 191 tokens、合计 1358 tokens；usage.cost合计 US$0.0003509。费用为接口返回值，最终以账户账单为准。

完整合成记录位于Git忽略的 `data/alternative-probe-20261017.json`。没有打印密钥，没有提交.env，也没有推送GitHub。

## 当前网页状态

本轮仅验证进程环境覆盖，因此当前8002网页仍是此前启动的Gemini配置。后续若要使用备选模型网页，应以LLM_MODEL环境变量重新启动该网页进程。模型工具闭环本轮已经验证，但不能将本结果扩大为完整应用全部场景验收。

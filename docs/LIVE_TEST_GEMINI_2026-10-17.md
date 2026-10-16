# OpenRouter Gemini 3 Flash Preview 联调记录

时间：2026-10-17，Asia/Shanghai。目标为按用户指定配置先验证最小响应，再验证真实工具闭环；不把无法执行的步骤算作通过。

## 配置与现有实现

- 请求模型ID：`google/gemini-3-flash-preview`。
- Base URL：`https://openrouter.ai/api/v1`。
- Settings自动归一化至：`https://openrouter.ai/api/v1/chat/completions`。
- 密钥：仅从项目根目录 `.env` 安全读取；没有打印、写入源码或提交。
- `.env` 中已更新LLM_ENDPOINT/LLM_MODEL，保留原有LLM_API_KEY；文件权限0600，Git忽略。
- 独立`Model` Protocol与`CompatibleModel`未改成固定路由，原有ReplayModel、ScriptedModel和自动化测试均保留。

现有provider发送非流式Chat Completions，工具路径使用`tools`和`tool_choice:auto`，校验assistant工具信封后由Agent执行并回传role=tool。检查发现没有基础地址重复拼接问题；本轮不需要重写provider或业务服务。

## 实际请求与结果

| 请求 | 内容 | 结果 |
|---|---|---|
| 推理1 | 经现有CompatibleModel，简单中文连接检查；tools为空列表 | HTTP403，无模型文本或工具响应 |
| 推理2 | 最小纯文本POST，无tools、tool_choice等任何工具参数 | 相同HTTP403，排除“只有工具格式才触发拒绝”的解释 |
| 只读认证检查 | GET `/api/v1/key`，只记录HTTP状态，不输出账户资料 | HTTP200，密钥认证有效；不是模型推理请求 |

两次推理均返回：

```json
{
  "error": {
    "message": "The request is prohibited due to a violation of provider Terms Of Service.",
    "code": 403,
    "metadata": {
      "provider_name": null,
      "previous_errors": [
        {
          "code": 403,
          "message": "The request is prohibited due to a violation of provider Terms Of Service."
        }
      ]
    }
  }
}
```

认证通过不等于目标模型推理权限通过。该响应未提供具体provider、地区、账户策略或路由诊断，不能据此认定是地区限制、Google上游拒绝、OpenRouter账户策略或其他确切原因。公开模型页显示模型存在，也不能替代该账户实际可用性的验证。

由于最小推理即被策略拒绝，已停止后续请求，不重复消耗调用：

- 基本文本响应：**未通过**。
- 真实工具选择及参数：**未执行**。
- 工具结果回传、后续工具调用：**未执行**。
- 模型最终自然语言回答：**未执行**。
- 真实模型日程CRUD：**未执行**；普通用户数据库未被本轮测试修改。

临时驱动设有最多10次推理请求硬上限和单次4096输出token上限，但此次只发送2次推理；另有1次只读认证GET。未切换其他模型假装目标模型成功，也未修改请求地区来绕过限制。

## API用量与费用

- 实际推理请求数：**2**。
- 认证检查请求数：**1**（非推理）。
- 成功推理数：**0**。
- 服务没有返回`usage`、实际response model或generation id；token和费用均为**未知/未提供**，不能将缺失值当作已确认0费用。
- 私有诊断记录：Git忽略的`data/gemini-probe-20261017.json`，包含脱敏错误和阶段状态，不含密钥。

## 最小处理建议

1. 在OpenRouter账户设置中核对该key的模型/提供商权限、账户策略、已设置的guardrails，以及当前账户和请求环境是否符合该模型及provider服务条款。
2. 联系OpenRouter支持并提供上述错误、模型ID、调用时间和`provider_name:null`。不向报告、仓库或工单公开API key。
3. 访问问题解决后先只重跑一个纯文本smoke；成功后再跑只读工具链（search_events→find_slots→最终中文回答），最后在独立合成SQLite上验证实际Agent。

当前最小修复在账户/服务可访问性层，而非硬编码Agent、删除工具参数或重写日历。若后续返回具体的schema/签名错误，再按证据修复provider并增加离线回归测试。403本身没有证据表明现有工具协议不兼容。[OpenRouter错误文档](https://openrouter.ai/docs/api/reference/errors-and-debugging)解释403可涵盖权限或策略拒绝，但无法替代这次缺失的具体原因。

## 离线回归

本轮配置前后保留所有mock接口。运行`RUN_LIVE_LLM=0 .venv/bin/python -m pytest -q`：**56 passed，2 skipped**。跳过的是需显式开启的真实模型pytest用例；本轮API探测是单独驱动，不能记为它们通过。

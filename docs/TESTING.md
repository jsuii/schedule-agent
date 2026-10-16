# 测试与验收记录

日期：2026-10-17（Asia/Shanghai）。环境：macOS arm64、Python 3.12.15、Node.js 26.12.0。没有配置真实模型，未发送任何真实 LLM API 请求，未访问外部日历账号。

## 测试策略

只替换不稳定的模型边界：ScriptedModel按工具协议返回预设消息，HTTPX MockTransport校验真实provider请求。真实Calendar、SQLite、工具执行、Agent循环、FastAPI与确认端点均参与测试。每项使用tmp_path独立文件，不修改普通用户日历。

模拟测试能证明工具选择后的执行、边界与恢复，**不能证明真实模型会做出同样决定、理解中文或兼容某个provider**。`test_live.py` 是另外的真实模型验收，默认跳过。没有以正则、规则分类器替代真实模型接口。

## 实际命令与结果

```bash
python -m pytest --cov=scheduler --cov-report=term-missing -q
ruff check scheduler tests
ruff format --check scheduler tests
node --check scheduler/static/app.js
```

最终本地与独立干净虚拟环境结果：**54 passed、2 skipped，0 failed；总Python语句覆盖率93%**。被跳过的两项都属于真实LLM联调。Ruff检查与格式检查、JavaScript语法检查通过。

干净环境的Starlette 1.8.0 TestClient产生一个依赖弃用提示：使用httpx的测试客户端未来建议改用httpx2；当前HTTPX 0.28.1协议测试及全部HTTP集成仍通过。没有过滤或隐藏该warning；迁移测试传输不是本轮功能前置条件。

此前错误及修复：首次测试因模块尚未实现而失败；短中文错字检索只比较完整标题导致漏召回，改为标题等宽窗口；浏览器发现确认后演示按钮未恢复，修复busy状态；独立代码复核复现澄清文本可夹带虚假成功声明、空档时间未显示，已以失败回归测试验证再修正；确认后清除旧候选卡片也有回归测试。当前没有未解决的失败用例。

## 自动化覆盖

| 文件 | 验证内容 | 模型 |
|---|---|---|
| test_calendar.py | 实际CRUD读回、版本、重启、UTC范围、半开相邻、重叠拒绝、并发原子冲突、标题/地点/备注/错字检索、SQL注入字符串、无效日期/时长、DST gap/fold、空档 | 不涉及LLM |
| test_provider.py | endpoint规范化、配置优先级、SecretStr、请求体/Authorization、401/400/429/500等分类、格式与超时、缺配置 | HTTP mock，无真实LLM |
| test_agent.py | 模型→工具→观察→模型、多轮跨重启补全、检索后修改、歧义拒绝及明确选择、未观测ID保护、冲突反馈后找空档、自由文本假成功丢弃、坏参数/未知工具/循环/超时 | 脚本替身 |
| test_web.py | 真实前端入口/HTTP提案确认、重复request回放、重启确认、跨会话/Origin/Host、过期、输入schema、并发确认一次、新消息失效、无配置真实错误 | 脚本替身；缺配置用真实provider配置检查（无网络） |
| test_demo.py | 完整HTTP中文演示脚本：澄清→创建→查询→修改→冲突→同名消歧→唯一删除；普通输入明确是固定脚本 | ReplayModel，不具备语言理解 |
| test_regressions.py | stale提案不覆盖新值、移时间保留时长、终止后同批工具不执行、无效日期、幂等中断、未知字段、澄清注入拦截、空档实际时间与空结果、确认清理旧候选 | 脚本/HTTP替身 |
| test_live.py | 真实中文相对日期与多轮、模糊目标检索后修改 | **未运行，默认跳过** |

## 验收场景检查

| 场景 | 证据与结论 |
|---|---|
| 清晰自然语言创建 | 完整HTTP脚本完成真实写入；自然语言解释由fixture预设，真实模型效果待测 |
| 日期/范围查询 | Calendar范围和Agent查询测试；浏览器显示实际保存数据 |
| 不完整请求及后续补全 | Agent持久化历史测试、浏览器定向追问和补充 |
| 模糊匹配后更新 | 工具返回候选与version后提案、确认读回 |
| 歧义目标 | 无法直接propose，必须选择或缩小查询；浏览器复现同名两条 |
| 唯一删除 | test_demo删除一个ID并验证另一条仍在；浏览器同流程 |
| 冲突及建议 | 真实拒绝重叠、回送冲突给模型、空档时间显示；浏览器复现 |
| 重启持久化 | 新Store、新FastAPI实例继续相同cookie/提案，事件不丢失 |
| 错误输入/未知工具 | schema拒绝，多步循环可修复，迭代上限可停止 |
| 模型超时/格式/工具失败 | HTTP/Agent故障替身验证安全错误，不宣告成功 |
| 模型错误声称成功 | 普通content丢弃；ask_user只接收枚举字段，虚假question参数拒绝 |
| 循环与批工具 | 最大步数、终止后同批跳过，测试有断言 |
| 秘钥 | SecretStr、错误不暴露body、Git忽略检查、全库秘钥模式检查 |
| 时区/重复/并发 | DST、UTC、请求幂等、重复确认、stale保护、原子冲突 |

## 浏览器和真实进程验证

通过本机浏览器访问实际监听127.0.0.1:8001的离线服务，手动驱动网页按钮：追问、补充、待确认创建、确认后查询、修改地点前后对照、冲突和具体替代时段、第二条同名日程、选择并确认删除。页面日程计数从0→1→2→1，仅预期记录受影响。手机宽度布局、输入、确认按钮、刷新后的持久化恢复均检查。

审查修复后重启独立新演示数据库，复验结构化澄清、创建与按钮恢复；完整修复后的HTTP场景由test_demo再次覆盖。截图见 [local-calendar.png](screenshots/local-calendar.png)。浏览器检查不等同自动化跨浏览器全覆盖；未测试所有桌面分辨率或无障碍读屏器。

干净安装：新建无system-site-packages的临时venv，按依赖声明安装成功，运行scripts/check.sh通过。普通模式也独立启动，未配置模型时正常显示就绪和配置提示。wheel打包成功并验证static/index.html、style.css、app.js均存在。初次尝试`--no-build-isolation`因干净环境无setuptools失败，改用项目声明的标准隔离构建后成功，未改变应用代码掩盖问题。

## 后续真实模型验收

配置步骤、兼容性和数据边界详见README。准备endpoint、model及API key，重启普通模式，再运行：

```bash
RUN_LIVE_LLM=1 python -m pytest -m live -v
```

使用合成日程和临时数据库，不访问真实日历。必须再手动检查冲突追问、多候选消歧、纠正前文、模型超时与不支持工具的错误。记录provider/model/日期及实际结果，不能直接沿用本次离线结论。真实模型质量、供应商端到端兼容性和GitHub分享权限当前都未验收。

# GitHub 提交前审查 · 2026-10-17

结论：本地检查未发现提交阻塞问题。保持功能冻结，本次只修正文档和忽略规则；没有暂存、提交或推送，没有修改 `.env`、原始题目、用户日历或个人编辑器文件。工作区保留前几轮未提交的功能修复，须将配套源码、测试及文档一起提交。

用户随后另行授权的小规模真实模型复验见[独立报告](PRE_SUBMIT_LIVE_2026-10-17.md)：基础推理及工具闭环通过，9次HTTP200；10次尝试预算用完后停止，澄清后提案尚未实测。下表“0调用”仅指此前离线审查阶段。

## 离线审查阶段实际验证

| 检查 | 结果 | 边界 |
|---|---|---|
| `PATH="$PWD/.venv/bin:$PATH" RUN_LIVE_LLM=0 sh scripts/check.sh` | 完整运行两次，均183通过、2跳过、0失败；覆盖率92% | 两项live默认跳过；本次真实模型请求0、tokens 0、费用0 |
| Ruff lint / format、JS语法 | 通过，37个Python文件符合格式 | 实际JS/DOM请求生命周期测试也由pytest运行 |
| 依赖锁定核对 | requirements.lock全部26项与本地安装版本一致 | 本次没有联网安装依赖，不声称全新机器已安装成功 |
| wheel构建 | `pip wheel --no-index --no-deps --no-build-isolation` 成功 | 使用已有本地构建依赖 |
| wheel独立目录安装及运行 | 临时target安装，离开源码目录导入；页面/JS/health/state HTTP200；创建、修改、软删除、恢复分别确认后成功；重新打开SQLite保留数据、schema=3 | 复用本地已锁定依赖；显式无模型配置，仅合成临时数据库 |
| 独立代码复核 | 未发现提交阻塞问题；独立重跑183通过、2跳过及静态检查 | 重点复核任务切换/恢复/取消、旧提案、失败状态、空档重验、回滚、迟到响应 |
| `git diff --check` | 通过 | 未执行Git写操作 |

构建产物：`schedule_agent_local-0.1.0-py3-none-any.whl`，SHA256 `756ade2db3a48b615b489c890fbfa37523e01d273d7dbd93cb9a49a7f267e13b`，输出在临时目录，不纳入提交。包内包含静态页面资源，不包含本地数据库、测试、环境密钥或编辑器设置。

临时打包检查首次因macOS `/tmp`解析为`/private/tmp`导致验收脚本路径断言失败；将断言改为两侧解析后的Path比较，随后完整通过。该失败不是应用缺陷，没有为此更改产品代码。

## 提交安全

- `.env`未被跟踪并命中忽略规则；`.env.example`密钥为空，JSON示例使用环境变量占位符。
- 当前候选文本及226个可达Git对象完成常见凭据模式和本机实际API key精确匹配，未发现泄漏。扫描不输出密钥、文件内容或原始provider响应；模式扫描不能证明不存在所有未知类型秘密。
- `data/`、SQLite、环境配置、缓存和构建目录已忽略；新增 `.vscode/` 忽略，原个人配置保留。截图为先前合成验收证据；二进制内容不依赖文本扫描证明安全。
- 当前没有staged文件，没有配置origin；未联网创建仓库或检查远程权限。

## 文档修正

README使用可移植Python3.12启动步骤，取消本机绝对路径与特定Conda环境要求；可复现启动使用自动创建的默认数据库，不要求本机验收文件。明确新克隆无密钥、模型权限取决于账户、旧live通过是历史证据。

需求、产品、架构、设计、演示、测试文档统一当前183/2/92%基线；schema修正为3，补充task_contexts及工具清单，校正挂起/恢复、批量取消和恢复改期规则。历史报告保留，不把之前的付费调用计为本次测试。README同时说明Node用于实际JS测试和语法检查。

## 建议提交范围

- 全部当前已跟踪修改：`.gitignore`、README、DESIGN、DEMO、工作日志、产品/需求/架构/测试文档、scheduler源码/静态页面和配套回归测试。
- 新源码：`scheduler/flow.py`。
- 新测试：`tests/test_clarification_ux.py`、`tests/test_shared_time.py`、`tests/test_task_contexts.py`、`tests/ui_clarification_check.cjs`、`tests/ui_context_check.cjs`。
- 新报告：本报告、PRE_SUBMIT_LIVE真实复验报告，以及CLARIFICATION_UX、CONTEXT_ISOLATION、RESTORE_FIX、SHARED_TIME_REGRESSION四份2026-10-17报告。
- 七张配套合成截图：clarification-next-step、clarification-to-confirmation、context-isolation-slots、flexible-slot-confirmation、model-reconnected、restore-conflict-completed、restore-conflict-options（均为docs/screenshots下PNG）。

不要提交 `.env`、`config.local.json`、data目录、数据库/账本、`.vscode/`、缓存或临时wheel。不要使用`git add .`后跳过暂存区核对。

## 剩余风险

后续真实复验已确认DeepSeek可用，但澄清后提案未在额度内完成；历史Gemini403未重试。模型语义判断仍非确定性，mock通过不能证明任意中文解析可靠。共享范围未明确分配时仍须用户补充；提醒不触发通知；已污染旧草稿不会自动拆分。应用仅支持本地单用户、单进程，不适合直接公网部署。现场演示、GitHub仓库发布和访问权限仍需实际完成；本次仅给出操作步骤。

## 后续由用户执行的GitHub步骤

先复核`git status --short`与`git diff`。按上面明确清单暂存；复核`git diff --cached --name-only`、`git diff --cached --check`及暂存内容，确认不含秘密或个人数据后再本地commit。首次推送需先创建目标仓库并配置origin，再由用户执行`git push -u origin main`，随后检查评审者访问权限。不要在remote URL中写访问令牌。本次未执行这些写操作。

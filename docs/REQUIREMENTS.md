# 需求分析与追踪

> 当前离线门禁：183通过、2个live跳过、覆盖率92%。任务隔离、灵活安排与提醒边界见[验收报告](CONTEXT_ISOLATION_2026-10-17.md)。历史真实模型结果与本次离线复核分别记录。
## 背景、用户与目标

面试编程题的本地日程助手，用户以中文口语描述计划；演示者和评审需要能解释模型的多步决策、确认操作和真实存储。首要目标是完整可演示的核心流程、可靠工具边界和可复现测试。

来源优先级：原始 `test.md` 是题目硬性要求；补充自主构建请求确定本次 CRUD、安全性、持久化、测试与文档范围；用户早期要求暂不配置真实模型，随后明确授权小规模OpenRouter联调及本轮自主改进；验收范围以本轮实际证据为准。题目附件中列举的示例用于需求分析，不作为执行外部发布、真实日历或消息操作的授权。

## 必须项与验收映射

| ID / 来源 | 要求与通过标准 | 实现 | 自动化证据 | 文档 / 当前状态 |
|---|---|---|---|---|
| R01 原题 | 中文网页输入与自然语言输出 | app.py、static/ | test_web、test_demo；浏览器完整演示 | README；本地通过 |
| R02 原题 | 必须用 LLM 理解自然语言；真正自主工具推理 | provider.py、agent.py、tools.py | test_agent 的多步 observation、test_provider 协议 | DESIGN §2；历史真实DeepSeek少量多步样例及两项live通过；本次离线审查跳过live，保留非确定性限制 |
| R03 原题 | endpoint/model/api_key 配置示例 | config.py、config.example.json、.env.example | test_provider 配置优先级/兼容错误 | README；离线通过 |
| R04 原题 | 明确关键推理/匹配/冲突/检索/交互决策 | DESIGN.md | 文档人工核对 | DESIGN 全部对应六类留白点 |
| R05 原题 | 完整前后端、依赖、启动说明 | pyproject.toml、requirements.lock、static/ | 干净 venv 安装、打包与启动检查 | README；本地通过 |
| R06 原题 | GitHub共享仓库且对评审开放 | 待外部发布 | 未推送/未开权限 | **未完成，需单独授权与目标仓库** |
| R07 原题 | 现场演示 | demo.py、网页 | test_demo + 实际浏览器检查 | README 演示步骤；离线可演示，少量真实模型已联调，现场验收未完成 |
| R08 补充 | 新建/日期区间查询/地点备注检索 | calendar.py、tools.py | test_calendar、test_demo | README；本地通过 |
| R09 补充 | 缺信息追问、跨轮补全、修正前文 | agent.py、SQLite turns | test_multiturn_clarification_persisted_across_restart | PRODUCT_DESIGN；替身协议测试通过 |
| R10 补充 | 更新/安全删除，影响正确条目 | observed candidates + propose/confirm | test_query_then_update、test_demo 的唯一删除 | DESIGN §4/6；本地通过 |
| R11 补充 | 模糊匹配；多候选必须消歧 | 字符窗口召回、候选选择门控 | test_short_fuzzy_keyword、test_ambiguous_target | ARCHITECTURE；本地通过 |
| R12 补充 | 冲突检测与替代建议 | UTC区间、slots、工具observation | overlap、atomic、conflict_feedback、slot_query | DESIGN §5；本地通过 |
| R13 补充 | 重启持久化、测试隔离 | SQLite 事件/会话/提案/请求 | test_crud_readback_persistence、test_full_http_flow | TESTING；通过 |
| R14 补充 | 验证实际结果，不接受模型成功幻觉 | 完整读回、结构化澄清、事实渲染 | test_model_claimed_success、test_clarification_cannot_smuggle_false_success | DESIGN §6/7；通过 |
| R15 补充 | 超时/错误/循环上限 | 30秒/90秒/8步限制 | test_timeout、loop_bound、provider_errors | README；通过 |
| R16 补充 | 重复/过期/并发/旧版本保护 | 事务、请求与提案幂等、版本 | test_concurrent_confirm、stale_update、expiry | ARCHITECTURE；通过 |
| R17 补充 | 秘钥排除/会话确认/跨站保护 | SecretStr、忽略配置、cookie、Origin/Host | test_provider_redacted、cross_session、schema字段禁止 | README；检查通过 |
| R18 补充 | 指定六份文档与追踪 | docs/*.md、README | 文档存在与内容人工复核 | 已提供 |
| R19 后续 | 无key继续；mock明确标注；保留真实接口 | Model Protocol，独立demo入口 | 离线套件与默认跳过live | TESTING；通过 |

## 功能与非功能边界

本次选择 CRUD 作为 MVP，原题并未强制所有示例；不把其开放项误称硬约束。具体事件必须有标题、日期时间和时长/结束时间；LLM 识别缺失项并选择澄清字段，程序不猜默认时长。修改仅改选中字段，移动开始默认保留原时长。

单用户本地日历；响应含中文文本；数据库失败不伪报成功；测试使用临时独立数据库。所有写入事务重查冲突、核对版本、读回验证。网页明确展示当前为离线脚本还是普通模型配置模式。

可维护性优先：一个 Python 服务、无 npm runtime、无外部日历服务。直接与完整传递依赖固定；工作区题目不覆盖。预算为约10小时主动开发的上限目标，没有人为等待占满预算。

## 假设与取舍

- 时区默认 Asia/Shanghai，可配置 IANA 时区；每轮当前时间都刷新。无偏移的 DST 重复/不存在本地时间拒绝。
- 短词相似度召回允许误召回；最终写入前消歧与确认。不声称任意语义同义词都能匹配。
- 重叠默认拒绝；明确可暂停/并行、双方允许重叠且核实注意力/通勤时可共存；原同地点规则之外，支持含往返的显式配对授权，地点不继承。其他情况需澄清或选择方案；没有强制覆盖。
- 多候选用户可缩小检索，或在卡片中选择；“第二个”等口语消歧需要真实模型选择更精确查询，未验证各模型表现。
- 历史只保留12轮；日程最多7天，空档31天、最多5个、15分钟步进；大数据量需进一步索引/分页。
- 原题的3天时限是背景，本轮执行按用户10小时目标自主收敛。

## 可选项 / 不在范围

周期日程、通知、邀请、Google Calendar、多用户权限、公网认证、节假日规则、长期记忆、embedding、Docker 与多 Agent 均不在 MVP 范围。UI不展示模型内部推理链。额外服务只会增加部署和权限成本。

## 验收结论边界

本地应用、离线及小规模真实DeepSeek流程以TESTING和工作日志记录为准；R02已有实际调用证据，但不是任意语言输入的全面保证。**R06共享仓库发布仍未完成**，需单独授权及目标仓库。Gemini因403未通过，不隐藏失败。

## 本轮新增验收映射

A–H见`tests/test_conflicts.py`及冲突验收记录：精确事实/多冲突、约束迁移、暂停共存与硬冲突对照、稳定方案UI、批量原子提案、版本/过期、严格参数错误、幂等及故障回滚。自动拆分和异地通勤求解未完成，不将字段声明当作完整求解能力。真实模型失败轨迹保留，最终固定冲突分支通过但仍有冗余工具调用。

## 多事件与UX增量

用户原句成为固定回归文本，A–J映射见UX_TEST_2026-10-17.md；13项新增离线测试与4组真实模型场景/浏览器分别记录。每事件位置、时长与约束独立，跨轮补全、增加第二项、暂停许可、顺序、旧方案失效均受验证；不要求拆成多次消息。默认冲突展示收敛，但统一确认/原子事务保留。

## 批量取消与7天恢复增量

A1–F30追踪见TRASH_TEST_2026-10-17.md及tests/test_trash.py：范围/排除/歧义/新鲜度/软删除/恢复冲突/undo/过期清理/并发幂等/故障回滚。新增32项隔离测试，既有109项未降低断言；工具允许集合仅增加3项。清理只在测试库执行，升级前永久删除无法恢复；无需手工SQL迁移。

## 任务隔离与提醒范围（2026-10-17）

新独立请求必须能挂起旧未完成任务，不继承其缺失字段；支持按真实任务ID显式继续/恢复/取消，歧义只问一次。旧确认在新消息处理期间不可执行，修改失败不恢复旧确认。灵活安排返回用户可选空档，选择重验后提案、确认后写入。提醒/通知/发邮件尚未支持，不以猜测时长伪装为事件；保留原请求并准确说明。具体限制与证据见[报告](CONTEXT_ISOLATION_2026-10-17.md)。

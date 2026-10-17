"""Validated tool surface. The model may propose but cannot authorize a mutation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scheduler.calendar import CalendarError, EventInput
from scheduler.storage import describe, describe_slots


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Search(Arguments):
    query: str = Field(default="", max_length=200)
    start: str | None = None
    end: str | None = None


class Slots(Arguments):
    start: str
    end: str
    duration_minutes: int = Field(ge=5, le=1440)
    exclude_id: str | None = None


class Question(Arguments):
    fields: list[
        Literal[
            "title", "date", "start", "duration", "selection", "alternative", "scope", "details"
        ]
    ] = Field(min_length=1, max_length=8)


class Patch(Arguments):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    start: str | None = None
    end: str | None = None
    location: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)

    importance: Literal["unknown", "high", "medium", "low"] | None = None
    flexibility: Literal["unknown", "fixed", "movable", "shortenable", "splittable"] | None = None
    interruptibility: (
        Literal["unknown", "noninterruptible", "pausable", "resumable", "parallel"] | None
    ) = None
    keep_time: bool | None = None
    minimum_minutes: int | None = Field(default=None, ge=1, le=10080)
    overlap_allowed: bool | None = None
    coexistence_verified: bool | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("至少提供一个修改字段")
        return self


class Update(Arguments):
    event_id: str
    patch: Patch


class Change(Arguments):
    action: Literal["create", "update"]
    event: EventInput | None = None
    event_id: str | None = Field(default=None, min_length=1, max_length=64)
    version: int | None = Field(default=None, ge=1)
    patch: Patch | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.action == "create":
            if (
                self.event is None
                or self.event_id is not None
                or self.version is not None
                or self.patch is not None
            ):
                raise ValueError("create 仅使用 event；title/start/end 必填")
        elif (
            self.event is not None
            or self.event_id is None
            or self.version is None
            or self.patch is None
        ):
            raise ValueError("update 使用 event_id、整数 version 与嵌套 patch；不需要完整 event")
        return self


class Resolution(Arguments):
    changes: list[Change] = Field(min_length=1, max_length=8)


class DraftIntent(Arguments):
    intent_id: str = Field(min_length=1, max_length=64)
    action: Literal["create", "update"] | None = None
    event_id: str | None = None
    version: int | None = Field(default=None, ge=1)
    event: Patch
    duration_minutes: int | None = Field(default=None, ge=1, le=10080)


class Relation(Arguments):
    first: str
    second: str
    kind: Literal["interrupt", "parallel", "sequential"] | None = None
    confirmed: bool | None = None
    travel_included: bool | None = None
    attention_verified: bool | None = None


class TimeWindow(Arguments):
    window_id: str = Field(min_length=1, max_length=64)
    intent_ids: list[str] = Field(min_length=2, max_length=8)
    start: str
    end: str


class TaskDraft(Arguments):
    intents: list[DraftIntent] = Field(default_factory=list, max_length=8)
    relations: list[Relation] = Field(default_factory=list, max_length=16)
    time_windows: list[TimeWindow] = Field(default_factory=list, max_length=8)


class Context(Arguments):
    action: Literal["new", "continue", "resume", "cancel", "clarify"]
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    entity: Literal["event", "reminder"] = "event"
    reminder_at: str | None = None
    notes: str = Field(default="", max_length=2000)


class Flexible(Arguments):
    title: str = Field(min_length=1, max_length=200)
    date: str
    duration_minutes: int = Field(ge=5, le=1440)
    not_before: str | None = None
    location: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=2000)


class CancelScope(Arguments):
    kind: Literal["today", "tomorrow", "week", "afternoon", "date", "interval"]
    date: str | None = None
    start: str | None = None
    end: str | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.kind == "date" and (not self.date or self.start or self.end):
            raise ValueError("date需要YYYY-MM-DD日期")
        if self.kind == "interval" and (not self.start or not self.end or self.date):
            raise ValueError("interval需要start/end")
        if self.kind not in ("date", "interval") and any((self.date, self.start, self.end)):
            raise ValueError("相对范围不能附加其他时间字段")
        return self


class CancelRange(Arguments):
    scope: CancelScope | None = None
    continue_previous: bool = False
    exclude_ids: list[str] = Field(default_factory=list, max_length=1000)
    exclude_queries: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def shape(self):
        if self.continue_previous == (self.scope is not None):
            raise ValueError("新请求提供scope；继续清单使用continue_previous=true且不传scope")
        return self


class TrashSearch(Arguments):
    query: str = Field(default="", max_length=200)
    deleted_start: str | None = None
    deleted_end: str | None = None


class Restore(Arguments):
    event_ids: list[str] = Field(default_factory=list, max_length=1000)
    operation_id: str | None = None
    latest: bool = False

    @model_validator(mode="after")
    def shape(self):
        if sum((bool(self.event_ids), self.operation_id is not None, self.latest)) != 1:
            raise ValueError("只提供观测event_ids、操作ID、或latest=true之一")
        return self


class Delete(Arguments):
    event_id: str


TOOLS = {
    "task_context": (
        Context,
        "区分任务边界：新独立请求用new并给title，先前任务自动挂起保留；补充当前任务用continue+真实task_id，恢复其他任务用resume+真实task_id，取消仅指定任务用cancel，真正不清楚是否换任务用clarify。不能把新请求并入旧草稿。提醒类用new entity=reminder，reminder_at和notes保留原意；系统不支持定时通知或发邮件。",
    ),
    "schedule_flexible": (
        Flexible,
        "用户授权寻找合适时间时使用。date为实际YYYY-MM-DD日期，下午固定12:00–18:00，duration_minutes为明确时长；not_before可传明确下界ISO。返回真实空档候选供网页选择，不自动选择或写入；不得用旧任务字段填新安排。",
    ),
    "propose_cancel_range": (
        CancelRange,
        "用户明确取消全部/某日期范围时使用，一次检索完整实际集合并只准备待确认清单，不要求逐条选择。scope.kind=today/tomorrow/week(周一至下周一)/afternoon(今天12点至18点)/date(YYYY-MM-DD)/interval(ISO start/end半开区间)。模型不构造删除清单。排除用唯一标题子串exclude_queries或清单真实exclude_ids；歧义问具体项。用户明确继续当前清单时continue_previous=true且不传scope，保留已排除项；新任务必须scope重新开始。",
    ),
    "search_trash": (
        TrashSearch,
        "查看回收站，可用query匹配标题，deleted_start/deleted_end过滤删除时间（半开区间）；返回真实ID、版本和来源批次供恢复，普通搜索不返回已删除事件。",
    ),
    "propose_restore": (
        Restore,
        "仅提出恢复，不直接写入。用户撤销刚才删除/恢复刚才全部时latest=true，来源本会话最近已提交删除；或提供已查询回收站的event_ids、或观测到的本会话删除operation_id。检查7天有效期与冲突，必要时用户在页面明确选择可行子集；绝不默默改时间或覆盖。",
    ),
    "stage_task": (
        TaskDraft,
        "一句话包含多个事件或需要多轮补全时，先用此工具保存所有独立意图（包括不完整事件）。intents用稳定intent_id，event必填且仅含该事件已知字段（如title/start/end）；不能只传ID。明确给定时长可用intent.duration_minutes，程序从start计算end。多个事件共享时间范围但分配未定时，用time_windows=[{window_id,intent_ids,start,end}]保留范围，不给每项复制完整区间、不均分时长；范围不是并行许可。未提地点留空，不继承其他事件地点。后续只传同intent_id的补充字段，不省略已知事件重新开始。关系first/second用intent_id；kind interrupt表示first可暂停容纳second，parallel表示并行，sequential表示first须先完成。仅用户明确允许时confirmed=true；往返包含在所填时间且注意力已核实时分别travel_included/attention_verified=true，否则不要猜测。只更新关系可传intents=[]。程序自动给窄澄清/冲突方案或统一提案，本轮结束。",
    ),
    "propose_resolution": (
        Resolution,
        "统一提出多个创建/修改，最终状态整体校验。changes 数组：create 用 event(title/start/end...)；update 用 event_id、整数 version、嵌套 patch(仅变更字段)。ID/version 必须来自本会话观测或当前冲突上下文。保留当前冲突中的新事件时必须包含 create，不得漏掉。不直接写入。",
    ),
    "search_events": (
        Search,
        "检索真实日程，按开始时间排序。query 支持标题、地点、备注子串及模糊匹配；可用 ISO start/end 限制范围（半开区间）。最多返回50条，有截断标记；修改/删除前必须调用。",
    ),
    "find_slots": (
        Slots,
        "在指定 ISO 时间范围内查找无冲突的时段，最多5个，每15分钟尝试；最多31天。移动已有事件可传 exclude_id。",
    ),
    "ask_user": (
        Question,
        "选择需要用户澄清的 fields：title 标题、date 日期、start 具体开始时间、duration 时长、selection 多候选选择、alternative 冲突替代、scope 不支持的范围、details 其他日程细节。程序结合真实工具结果生成中文问题，本轮立即结束；不接收自由文本或操作成功声明。",
    ),
    "propose_create": (
        EventInput,
        "提出新建日程，title/start/end 必填，禁止猜测缺失的时长。校验冲突，只生成待确认提案，绝不写入日程。本轮结束。",
    ),
    "propose_update": (
        Update,
        "提出修改唯一已检索目标；多候选需要用户先选择。只提供修改字段，单改 start 会保留原时长。生成待确认提案，本轮结束。",
    ),
    "propose_delete": (
        Delete,
        "提出删除唯一已检索目标；多候选需要用户先选择。只生成待确认提案，本轮结束。",
    ),
}


def public_schema(model):
    schema = model.model_json_schema()

    def strip_private(node):
        if isinstance(node, dict):
            node.get("properties", {}).pop("coexistence_groups", None)
            for child in node.values():
                strip_private(child)
        elif isinstance(node, list):
            for child in node:
                strip_private(child)

    strip_private(schema)
    if model is CancelRange:
        schema["properties"]["scope"] = {
            **schema["$defs"]["CancelScope"],
            "description": 'JSON object，例如 {"kind":"today"}。继续清单时省略，不编码为字符串。',
        }
    return schema


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": public_schema(schema),
        },
    }
    for name, (schema, description) in TOOLS.items()
]


class Tools:
    def __init__(self, store, sid, now=None, require_context=False, paused_proposal_id=None):
        self.now = now
        self.context_bound = not require_context
        self.paused_proposal_id = paused_proposal_id
        self.store, self.sid, self.cal = store, sid, store.calendar
        self.state = store.session(sid)["state"]
        # A card click authorizes selection for one model turn, not every future task.
        # Keep it in this toolset while consuming it from persistent session state.
        if self.state.get("selected_id"):
            self._save_state()
        self.last_conflicts = []
        self.last_slots = None  # None means unqueried, [] means verified empty.

    def _save_state(self):
        self.store.save_state(self.sid, {k: v for k, v in self.state.items() if k != "selected_id"})

    def execute(self, name, arguments):
        if name not in TOOLS:
            return {"error": "unknown_tool", "hint": "请使用提供的工具名称"}
        private_events = []
        if isinstance(arguments, dict):
            if name == "propose_create":
                private_events.append(arguments)
            elif name == "propose_resolution" and isinstance(arguments.get("changes"), list):
                private_events.extend(
                    c.get("event", {}) for c in arguments["changes"] if isinstance(c, dict)
                )
        if any(isinstance(e, dict) and "coexistence_groups" in e for e in private_events):
            return {
                "error": "invalid_arguments",
                "hint": "配对授权由服务端生成；请用stage_task说明用户明确的事件关系，不得传入内部授权字段",
            }
        try:
            value = TOOLS[name][0].model_validate(arguments)
        except ValidationError as exc:
            return {
                "error": "invalid_arguments",
                "hint": (
                    'scope 必须为 JSON object，例如 {"scope":{"kind":"today"}}，不能编码成字符串；继续清单用 {"continue_previous":true} 并省略 scope'
                    if name == "propose_cancel_range"
                    else "stage_task 每条intent必须提供event对象保存用户已给的title/start/end等字段，不能只传intent_id。共用但未分配的范围放time_windows（window_id,intent_ids,start,end），明确时长放duration_minutes；重新从原始消息提取，勿向用户重复索取已给信息。"
                    if name == "stage_task"
                    else "参数不符合工具 schema；propose_update 使用 {event_id, patch:{变更字段}}；propose_resolution 的 update 还需要整数 version。不要猜测 ID 或缺失信息"
                ),
                "issues": [
                    {"path": ".".join(str(p) for p in e["loc"])[:200], "type": e["type"]}
                    for e in exc.errors(
                        include_input=False, include_context=False, include_url=False
                    )[:8]
                ],
            }
        except TypeError:
            return {"error": "invalid_arguments", "hint": "工具参数必须是 JSON object"}
        try:
            if not self.context_bound and name not in ("task_context", "ask_user"):
                raise CalendarError(
                    "context_required",
                    hint="先用task_context决定continue当前实际task_id、new独立任务、resume其他任务或clarify歧义；不能把新请求塞进旧草稿",
                )
            return self._execute(name, value)
        except CalendarError as exc:
            if exc.code == "conflict":
                self.last_conflicts = exc.details.get("events", [])
                self.last_slots = exc.details.get("alternatives", [])
            return {"error": exc.code, **exc.details}
        except ValueError:
            return {"error": "invalid_arguments", "hint": "日期、时长或字段无效，请向用户核实"}

    def _execute(self, name, value):
        if name == "task_context":
            from scheduler.flow import control

            return control(self, value)
        if name == "schedule_flexible":
            from scheduler.flow import flexible

            return flexible(self, value)
        if self.state.get("slot_review") and (name.startswith("propose_") or name == "stage_task"):
            raise CalendarError(
                "slot_selection_required",
                hint="该任务正在等待用户选择空档；不能由模型自行选第一个时段提案。请让用户点击候选，或先明确新建其他任务",
            )
        if self.state.get("reminder") and name in (
            "stage_task",
            "propose_create",
            "propose_resolution",
        ):
            raise CalendarError(
                "unsupported_entity",
                hint="当前是未支持的提醒任务，不要补造事件时长；改成日历事件需新建明确event任务",
            )
        if name == "propose_cancel_range":
            from scheduler.trash import propose_cancel

            result = propose_cancel(
                self.store,
                self.sid,
                value.scope.model_dump(exclude_none=True) if value.scope else None,
                now=self.now,
                exclude_ids=value.exclude_ids,
                exclude_queries=value.exclude_queries,
                continue_previous=value.continue_previous,
            )
            self.state = self.store.session(self.sid)["state"]
            if result is None:
                return {"question": "指定范围内没有需要取消的日程，未作任何修改。"}
            if "question" in result:
                return result
            return {"proposal": result}
        if name == "search_trash":
            events = self.cal.trash(**value.model_dump(), limit=1001)
            self.state["trash_candidates"] = events[:1000]
            self._save_state()
            return {
                "view": "trash",
                "events": events[:1000],
                "truncated": len(events) > 1000,
                "message": f"回收站找到{len(events[:1000])}项"
                + ("（仅前1000项，请缩小条件）" if len(events) > 1000 else "")
                + "，请在回收站面板查看或恢复。",
            }
        if name == "propose_restore":
            from scheduler.trash import propose_restore

            operation = value.operation_id
            if value.latest:
                last = self.store.last_deletion(self.sid)
                if not last:
                    raise CalendarError("operation_not_found", hint="本会话没有可撤销的已提交删除")
                operation = last["operation_id"]
            expected = None
            if value.event_ids:
                observed = self.state.get("trash_candidates", [])
                expected = [e for e in observed if e["id"] in value.event_ids]
                if {e["id"] for e in expected} != set(value.event_ids):
                    raise CalendarError("not_observed", hint="请先查询回收站实际目标")
            return {
                "proposal": propose_restore(
                    self.store,
                    self.sid,
                    event_ids=value.event_ids,
                    operation_id=operation,
                    expected=expected,
                )
            }

        if name == "stage_task":
            from scheduler.tasks import stage

            return stage(self, value)

        if self.state.get("task") and name.startswith("propose_"):
            raise CalendarError(
                "pending_task",
                hint="当前任务有草稿；同一任务用stage_task补充，独立请求先task_context new挂起旧任务，不要合并无关事件",
            )
        if name == "propose_resolution":
            from scheduler.conflicts import validate_case, validate_resolution

            case = self.state.get("conflict")
            if case:
                validate_case(self.store, case)
            observed = self.state.get("candidates", []) + (case["snapshots"] if case else [])
            changes = value.model_dump(exclude_none=True)["changes"]
            for change in changes:
                if change["action"] == "update" and not any(
                    e["id"] == change["event_id"] and e["version"] == change["version"]
                    for e in observed
                ):
                    raise CalendarError(
                        "not_observed", hint="ID 与整数版本必须来自当前检索或冲突上下文"
                    )
            if case:
                validate_resolution(self.store, case, changes)
            try:
                self.cal.prepare_batch(changes)
            except CalendarError as exc:
                if exc.code == "conflict" and len(changes) == 1:
                    from scheduler.conflicts import analyze

                    change = changes[0]
                    before = (
                        self.cal.get(change["event_id"]) if change["action"] == "update" else None
                    )
                    requested = (
                        {**{k: before[k] for k in EventInput.model_fields}, **change["patch"]}
                        if before
                        else change["event"]
                    )
                    normalized = self.cal.validate(requested)
                    requested = {
                        **normalized,
                        "start": self.cal.iso(normalized["start"]),
                        "end": self.cal.iso(normalized["end"]),
                    }
                    case = analyze(self.store, self.sid, requested, before)
                    self.state["conflict"] = case
                    self.last_slots = []
                    raise CalendarError(
                        "conflict",
                        events=exc.details.get("events", []),
                        conflict=case,
                        alternatives=[],
                        hint="请从冲突面板选择方案或使用自定义调整；尚未修改日程",
                    ) from exc
                raise
            return {
                "proposal": self.store.propose(
                    self.sid, "batch", {"changes": changes, "context": case}
                )
            }

        if name == "ask_user":
            if "alternative" in value.fields and self.last_slots is None:
                return {
                    "error": "missing_observation",
                    "hint": "尚未查询空档，不能声称没有空档。请先调用propose工具校验冲突，或调用find_slots查询，再追问替代时间。",
                }
            parts = []
            labels = {
                "title": "日程标题",
                "date": "具体日期",
                "start": "开始时间",
                "duration": "时长或结束时间",
                "details": "具体日程需求",
            }
            missing = [labels[f] for f in value.fields if f in labels]
            if missing:
                parts.append("请补充" + "、".join(dict.fromkeys(missing)) + "？")
            if "selection" in value.fields:
                parts.append(
                    "请确认要处理哪一条日程；如果有多个候选，请选择下方卡片或补充日期、标题。"
                )
            if "alternative" in value.fields:
                if self.last_conflicts:
                    parts.append(
                        "指定时间与以下日程冲突：\n"
                        + "\n".join(describe(e) for e in self.last_conflicts)
                    )
                if self.state.get("conflict"):
                    parts.append(
                        "请查看冲突面板的已校验方案；若需保留重叠，请明确可暂停性、地点、注意力和通勤条件。"
                    )
                else:
                    parts.append(describe_slots(self.last_slots))
                parts.append("你希望采用哪个时段，或另选什么时间？日程尚未改变。")
            if "scope" in value.fields:
                parts.append(
                    "目前支持单次本地日程的查询、添加、修改和删除；暂不支持周期、邀请或外部日历同步。是否改为单次日程？"
                )
            return {"question": "\n".join(parts)}
        if name == "search_events":
            events = self.cal.search(**value.model_dump(), limit=51)
            selected = self.state.get("selected_id")
            previous = next(
                (e for e in self.state.get("candidates", []) if e["id"] == selected), None
            )
            self.state = {
                **{k: v for k, v in self.state.items() if k != "selected_id"},
                "candidates": events[:50],
                "truncated": len(events) > 50,
                "search": value.model_dump(),
            }
            if previous and any(
                e["id"] == selected and e["version"] == previous["version"] for e in events[:50]
            ):
                self.state["selected_id"] = selected
            self._save_state()
            return {
                "events": events[:50],
                "truncated": len(events) > 50,
                "hint": "结果不唯一时请追问；截断时请缩小范围",
            }
        if name == "find_slots":
            self.last_slots = self.cal.slots(**value.model_dump())
            return {"slots": self.last_slots}
        before = None
        if name == "propose_create":
            payload = value.model_dump()
        else:
            candidates = self.state.get("candidates", [])
            if self.state.get("search") is not None:
                current = self.cal.search(**self.state["search"], limit=51)
                if (len(current) > 50) != bool(self.state.get("truncated")) or {
                    (e["id"], e["version"]) for e in current[:50]
                } != {(e["id"], e["version"]) for e in candidates}:
                    raise CalendarError(
                        "stale_candidates", hint="检索结果已变化，请重新检索并确认目标"
                    )
            before = next((e for e in candidates if e["id"] == value.event_id), None)
            if before is None:
                raise CalendarError("not_observed", hint="请先使用 search_events 检索实际目标")
            if (len(candidates) > 1 or self.state.get("truncated")) and self.state.get(
                "selected_id"
            ) != value.event_id:
                raise CalendarError(
                    "ambiguous", events=candidates, hint="请让用户选择候选卡片或缩小检索范围"
                )
            actual = self.cal.get(value.event_id)
            if actual is None or actual["version"] != before["version"]:
                raise CalendarError("stale")
            payload = {k: before[k] for k in EventInput.model_fields}
            if name == "propose_update":
                patch = value.patch.model_dump(exclude_none=True)
                if "start" in patch and "end" not in patch:
                    duration = self.cal.timestamp(before["end"]) - self.cal.timestamp(
                        before["start"]
                    )
                    patch["end"] = self.cal.iso(self.cal.timestamp(patch["start"]) + duration)
                payload.update(patch)
        if name != "propose_delete":
            case = self.state.get("conflict")
            if case and (
                (before and case.get("before") and before["id"] == case["before"]["id"])
                or (
                    not before
                    and not case.get("before")
                    and payload["title"] == case["requested"]["title"]
                )
            ):
                from scheduler.conflicts import validate_resolution

                change = (
                    {
                        "action": "update",
                        "event_id": before["id"],
                        "version": before["version"],
                        "patch": payload,
                    }
                    if before
                    else {"action": "create", "event": payload}
                )
                validate_resolution(self.store, case, [change])
            normalized = self.cal.validate(payload)
            payload = {
                **normalized,
                "start": self.cal.iso(normalized["start"]),
                "end": self.cal.iso(normalized["end"]),
            }
            conflicts = self.cal.conflicts(
                payload["start"], payload["end"], before["id"] if before else None
            )
            conflicts = [e for e in conflicts if not self.cal.compatible(payload, e)]
            if before:
                self.cal.check_constraints(before, payload)
            if conflicts:
                stop = self.cal.iso(self.cal.timestamp(payload["start"]) + 86400)
                duration = max(5, (normalized["end"] - normalized["start"]) // 60)
                alternatives = (
                    self.cal.slots(
                        payload["start"], stop, duration, before["id"] if before else None
                    )
                    if duration <= 1440
                    else []
                )
                from scheduler.conflicts import analyze

                case = analyze(self.store, self.sid, payload, before)
                self.state["conflict"] = case
                raise CalendarError(
                    "conflict",
                    conflict=case,
                    events=conflicts,
                    alternatives=alternatives,
                    hint="不要自动改动用户指定时间；解释冲突并询问替代时段",
                )
        action = name.removeprefix("propose_")
        return {"proposal": self.store.propose(self.sid, action, payload, before)}

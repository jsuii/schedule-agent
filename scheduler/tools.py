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

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("至少提供一个修改字段")
        return self


class Update(Arguments):
    event_id: str
    patch: Patch


class Delete(Arguments):
    event_id: str


TOOLS = {
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
SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema.model_json_schema(),
        },
    }
    for name, (schema, description) in TOOLS.items()
]


class Tools:
    def __init__(self, store, sid):
        self.store, self.sid, self.cal = store, sid, store.calendar
        self.state = store.session(sid)["state"]
        self.last_conflicts = []
        self.last_slots = []

    def execute(self, name, arguments):
        if name not in TOOLS:
            return {"error": "unknown_tool", "hint": "请使用提供的工具名称"}
        try:
            value = TOOLS[name][0].model_validate(arguments)
        except ValidationError as exc:
            return {
                "error": "invalid_arguments",
                "hint": "参数不符合工具 schema；请补全信息或修正，不要猜测",
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
            return self._execute(name, value)
        except CalendarError as exc:
            if exc.code == "conflict":
                self.last_conflicts = exc.details.get("events", [])
                self.last_slots = exc.details.get("alternatives", [])
            return {"error": exc.code, **exc.details}
        except ValueError:
            return {"error": "invalid_arguments", "hint": "日期、时长或字段无效，请向用户核实"}

    def _execute(self, name, value):
        if name == "ask_user":
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
                parts.append(describe_slots(self.last_slots))
                parts.append("你希望采用哪个时段，或另选什么时间？日程尚未改变。")
            if "scope" in value.fields:
                parts.append(
                    "目前支持单次本地日程的查询、添加、修改和删除；暂不支持周期、邀请或外部日历同步。是否改为单次日程？"
                )
            return {"question": "\n".join(parts)}
        if name == "search_events":
            events = self.cal.search(**value.model_dump(), limit=51)
            self.state = {"candidates": events[:50], "truncated": len(events) > 50}
            self.store.save_state(self.sid, self.state)
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
            payload = {k: before[k] for k in ["title", "start", "end", "location", "notes"]}
            if name == "propose_update":
                patch = value.patch.model_dump(exclude_none=True)
                if "start" in patch and "end" not in patch:
                    duration = self.cal.timestamp(before["end"]) - self.cal.timestamp(
                        before["start"]
                    )
                    patch["end"] = self.cal.iso(self.cal.timestamp(patch["start"]) + duration)
                payload.update(patch)
        if name != "propose_delete":
            normalized = self.cal.validate(payload)
            payload = {
                **normalized,
                "start": self.cal.iso(normalized["start"]),
                "end": self.cal.iso(normalized["end"]),
            }
            conflicts = self.cal.conflicts(
                payload["start"], payload["end"], before["id"] if before else None
            )
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
                raise CalendarError(
                    "conflict",
                    events=conflicts,
                    alternatives=alternatives,
                    hint="不要自动改动用户指定时间；解释冲突并询问替代时段",
                )
        action = name.removeprefix("propose_")
        return {"proposal": self.store.propose(self.sid, action, payload, before)}

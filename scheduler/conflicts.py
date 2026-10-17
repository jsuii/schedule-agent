"""Deterministic facts and small, final-state-validated alternatives. No language routing."""

import time
from uuid import uuid4

from scheduler.calendar import CalendarError, EventInput


def analyze(store, sid, requested, before=None):
    cal = store.calendar
    overlaps = cal.conflicts(requested["start"], requested["end"], before["id"] if before else None)
    overlaps = [e for e in overlaps if not cal.compatible(requested, e)]
    facts = []
    for old in overlaps:
        facts.append(
            {
                "requested": requested,
                "existing": old,
                "overlap": {
                    "start": cal.iso(
                        max(cal.timestamp(requested["start"]), cal.timestamp(old["start"]))
                    ),
                    "end": cal.iso(min(cal.timestamp(requested["end"]), cal.timestamp(old["end"]))),
                },
                "classification": "hard_conflict"
                if old["interruptibility"] == "noninterruptible" or old["flexibility"] == "fixed"
                else "needs_clarification",
                "question": "能否暂停或并行？需明确地点一致、注意力和通勤条件后才能保留重叠。",
            }
        )
    initial = (
        {
            "action": "update",
            "event_id": before["id"],
            "version": before["version"],
            "patch": requested,
        }
        if before
        else {"action": "create", "event": requested}
    )
    case = {
        "id": uuid4().hex,
        "expires": time.time() + 900,
        "timezone": str(cal.tz),
        "requested": requested,
        "before": before,
        "facts": facts,
        "plans": [],
        "snapshots": overlaps + ([before] if before else []),
        "status": "尚未修改日程",
    }

    def offer(title, changes, reason):
        try:
            prepared = cal.prepare_batch(changes)
        except (CalendarError, ValueError):
            return
        case["plans"].append(
            {
                "id": uuid4().hex,
                "title": title,
                "changes": changes,
                "events": prepared,
                "reason": reason,
                "recommended": not case["plans"],
                "hard_constraints_satisfied": True,
                "residual_conflicts": [],
                "assumptions": ["仅验证本地日历；外部预约可调整性、准备及通勤需自行核实。"],
                "requires_confirmation": True,
            }
        )

    # Two distinct directions, rather than a row of 15-minute variants.
    if requested.get("flexibility") != "fixed" and not requested.get("keep_time"):
        duration = cal.timestamp(requested["end"]) - cal.timestamp(requested["start"])
        for direction, label in (
            (1, "保留原安排，将新安排移到之后"),
            (-1, "保留原安排，将新安排提前"),
        ):
            anchor = (
                max(cal.timestamp(e["end"]) for e in overlaps)
                if direction == 1
                else min(cal.timestamp(e["start"]) for e in overlaps) - duration
            )
            for step in range(25):
                start = anchor + direction * step * 3600
                adjusted = {**requested, "start": cal.iso(start), "end": cal.iso(start + duration)}
                change = {**initial, "patch" if before else "event": adjusted}
                count = len(case["plans"])
                offer(label, [change], "保留已有安排，只调整一条；时间与外部条件等待你确认。")
                if len(case["plans"]) != count:
                    break
    # Keep requested times; move all overlapping originals together, validating ALL other events.
    cursor = cal.timestamp(requested["end"])
    changes = [initial]
    for old in overlaps:
        duration = cal.timestamp(old["end"]) - cal.timestamp(old["start"])
        changes.append(
            {
                "action": "update",
                "event_id": old["id"],
                "version": old["version"],
                "patch": {"start": cal.iso(cursor), "end": cal.iso(cursor + duration)},
            }
        )
        cursor += duration
    offer("保留新安排，顺延冲突日程", changes, "保留本次指定时间；需要逐项确认原安排能否移动。")
    if overlaps and all(e["flexibility"] == "shortenable" for e in overlaps):
        shortened = [initial] + [
            {
                "action": "update",
                "event_id": e["id"],
                "version": e["version"],
                "patch": {"end": requested["start"]},
            }
            for e in overlaps
        ]
        offer(
            "保留新安排，缩短可调整时长的日程",
            shortened,
            "只缩短明确允许调整的活动，并验证最短持续时间。",
        )
    weights = {"high": 8, "medium": 4, "low": 1, "unknown": 2}

    def disruption(plan):
        score = 0
        for item in plan["events"]:
            original = item["before"] or requested
            if any(
                cal.timestamp(item["after"][k]) != cal.timestamp(original[k])
                for k in ("start", "end")
            ):
                score += weights[original["importance"]]
        return score, len(plan["changes"])

    case["plans"].sort(key=disruption)
    case["plans"] = case["plans"][:4]
    for i, plan in enumerate(case["plans"]):
        plan["recommended"] = i == 0
        if i == 0:
            plan["reason"] += " 按明确的重要性减少时间变动；重要性未知时优先减少修改条数。"
    state = store.session(sid)["state"]
    state["conflict"] = case
    store.save_state(sid, state)
    return case


def validate_case(store, case, conn=None):
    if not case or case["expires"] <= time.time():
        raise CalendarError("plan_expired", hint="冲突方案已过期，请重新提出安排")
    for expected in case["snapshots"]:
        actual = store.calendar.get(expected["id"], conn)
        if not actual or actual["version"] != expected["version"]:
            raise CalendarError("stale_plan", hint="相关日程已变化，旧方案失效，请重新分析")


def validate_resolution(store, case, changes, conn=None, sid=None):
    """Bind the requested operation and its hard times to the proposed final set."""
    validate_case(store, case, conn)
    if case.get("kind") == "task":
        from scheduler.tasks import validate_task

        validate_task(store, case["task"], changes, conn, sid=sid)
        return
    requested, before = case["requested"], case.get("before")
    matches = [
        c
        for c in changes
        if (c["action"] == "update" and before and c["event_id"] == before["id"])
        or (c["action"] == "create" and not before and c["event"]["title"] == requested["title"])
    ]
    if len(matches) != 1:
        raise CalendarError(
            "incomplete_resolution",
            hint="统一提案必须包含当前冲突中的原请求；新增用同一标题 event，修改用原 event_id。取消原请求请开始新对话。",
        )
    change = matches[0]
    after = (
        {**{k: before[k] for k in EventInput.model_fields}, **change["patch"]}
        if before
        else EventInput.model_validate(change["event"]).model_dump()
    )
    store.calendar.check_constraints(requested, after)
    # Keep hard metadata as well as the time. A second turn cannot silently remove it.
    if requested.get("keep_time") and not after.get("keep_time"):
        raise CalendarError("hard_constraint", hint="原请求要求保留时间，提案必须保留 keep_time")
    if requested.get("flexibility") == "fixed" and after.get("flexibility") != "fixed":
        raise CalendarError("hard_constraint", hint="不得默默解除当前请求的固定时间约束")

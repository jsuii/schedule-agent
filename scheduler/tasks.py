"""Small persistent multi-event drafts; the model extracts intent, code validates state."""

import copy
import time
from uuid import uuid4

from scheduler.calendar import CalendarError, EventInput


def normalized(cal, event):
    data = cal.validate(event)
    return {**data, "start": cal.iso(data["start"]), "end": cal.iso(data["end"])}


def validate_task(store, task, changes, conn=None, sid=None):
    if sid is not None:
        if conn is None:
            state = store.session(sid)["state"]
        else:
            import json

            state = json.loads(
                conn.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()["state"]
            )
        current = state.get("task")
        if (
            not current
            or current["id"] != task["id"]
            or current.get("revision") != task.get("revision")
        ):
            raise CalendarError("stale_task", hint="任务已有新补充，旧方案失效，请使用最新方案")
    if task.get("source_context"):
        from scheduler.conflicts import validate_resolution

        validate_resolution(store, task["source_context"], changes, conn)

    if task["expires"] <= time.time():
        raise CalendarError("task_expired", hint="任务已过期，请重新描述安排")
    if len(changes) != len(task["intents"]):
        raise CalendarError("incomplete_task", hint="统一提案必须保留全部事件")
    indexed = {c.get("intent_id"): c for c in changes}
    if len(indexed) != len(changes):
        raise CalendarError("incomplete_task")
    after = {}
    for intent in task["intents"]:
        c = indexed.get(intent["intent_id"])
        if not c or c["action"] != intent["action"]:
            raise CalendarError("incomplete_task")
        if c["action"] == "update":
            if c["event_id"] != intent["event_id"] or c["version"] != intent["version"]:
                raise CalendarError("stale_task")
            before = store.calendar.get(c["event_id"], conn)
            if not before or before["version"] != c["version"]:
                raise CalendarError("stale_task")
            event = {**{k: before[k] for k in EventInput.model_fields}, **c["patch"]}
        else:
            event = EventInput.model_validate(c["event"]).model_dump()
        original = intent["event"]
        if event["title"] != original["title"]:
            raise CalendarError("incomplete_task", hint="原任务中的事件标题不匹配")
        store.calendar.check_constraints(original, event)
        if original.get("keep_time") and not event.get("keep_time"):
            raise CalendarError("hard_constraint")
        after[intent["intent_id"]] = event
    validate_windows(store.calendar, task, after)
    for relation in task["relations"]:
        if relation.get("kind") == "sequential":
            a, b = after[relation["first"]], after[relation["second"]]
            if store.calendar.timestamp(a["end"]) > store.calendar.timestamp(b["start"]):
                raise CalendarError("sequence_conflict", hint="需要按指定顺序依次完成")
    return after


def validate_windows(cal, task, events):
    for window in task.get("time_windows", []):
        begin, finish = cal.timestamp(window["start"]), cal.timestamp(window["end"])
        if finish <= begin:
            raise CalendarError("invalid_duration")
        for identifier in window["intent_ids"]:
            event = events[identifier]
            if any(
                event.get(field)
                and not (
                    begin <= cal.timestamp(event[field]) < finish
                    if field == "start"
                    else begin < cal.timestamp(event[field]) <= finish
                )
                for field in ("start", "end")
            ):
                raise CalendarError(
                    "outside_shared_window", hint="安排超出用户给定的共享时间范围，请先明确修改范围"
                )


def changes_for(task):
    changes = []
    for intent in task["intents"]:
        change = {"action": intent["action"], "intent_id": intent["intent_id"]}
        if intent["action"] == "create":
            change["event"] = intent["event"]
        else:
            change.update(
                event_id=intent["event_id"], version=intent["version"], patch=intent["event"]
            )
        changes.append(change)
    return changes


def build_case(store, task, changes):
    cal = store.calendar
    proposed = [c["event"] if c["action"] == "create" else c["patch"] for c in changes]
    excluded = {i["event_id"] for i in task["intents"] if i["action"] == "update"}
    existing = [e for e in cal.search(limit=100000) if e["id"] not in excluded]
    facts = []
    for n, a in enumerate(proposed):
        for b in proposed[:n] + existing:
            start = max(cal.timestamp(a["start"]), cal.timestamp(b["start"]))
            end = min(cal.timestamp(a["end"]), cal.timestamp(b["end"]))
            if start < end and not cal.compatible(a, b):
                facts.append(
                    {
                        "requested": a,
                        "existing": b,
                        "overlap": {"start": cal.iso(start), "end": cal.iso(end)},
                        "classification": "hard_conflict"
                        if any(
                            e["interruptibility"] == "noninterruptible"
                            or e["flexibility"] == "fixed"
                            for e in (a, b)
                        )
                        else "needs_clarification",
                        "question": "可以短暂暂停吗？寄送等外出时间是否包含往返，期间注意力是否可兼顾？",
                    }
                )
    case = {
        "id": uuid4().hex,
        "expires": task["expires"],
        "timezone": str(cal.tz),
        "kind": "task",
        "task": copy.deepcopy(task),
        "facts": facts,
        "plans": [],
        "snapshots": [e for e in existing if any(f["existing"].get("id") == e["id"] for f in facts)]
        + [store.calendar.get(i["event_id"]) for i in task["intents"] if i["action"] == "update"],
        "status": "尚未修改日程",
    }
    # Move a single flexible intent to either side of the affected interval. All other
    # intents are retained and the whole final set is validated, not just the first pair.
    for index, intent in enumerate(task["intents"]):
        original = intent["event"]
        if original["keep_time"] or original["flexibility"] == "fixed":
            continue
        others = proposed[:index] + proposed[index + 1 :] + existing
        collisions = [
            e
            for e in others
            if cal.timestamp(e["start"]) < cal.timestamp(original["end"])
            and cal.timestamp(e["end"]) > cal.timestamp(original["start"])
        ]
        if not collisions:
            continue
        duration = cal.timestamp(original["end"]) - cal.timestamp(original["start"])
        for direction in (1, -1):
            anchor = (
                max(cal.timestamp(e["end"]) for e in collisions)
                if direction == 1
                else min(cal.timestamp(e["start"]) for e in collisions) - duration
            )
            for step in range(25):
                trial = copy.deepcopy(changes)
                event = trial[index]["event" if intent["action"] == "create" else "patch"]
                start = anchor + direction * step * 3600
                event.update(start=cal.iso(start), end=cal.iso(start + duration))
                try:
                    validate_task(store, task, trial)
                    preview = cal.prepare_batch(trial)
                except (CalendarError, ValueError):
                    continue
                case["plans"].append(
                    {
                        "id": uuid4().hex,
                        "title": f"{original['title']}改到{cal.iso(start)[11:16]}–{cal.iso(start + duration)[11:16]}",
                        "reason": "只移动一项，保留其余安排。",
                        "recommended": not case["plans"],
                        "changes": trial,
                        "events": preview,
                        "assumptions": ["仅本地安排；外部预约及出行条件需核实。"],
                        "hard_constraints_satisfied": True,
                        "residual_conflicts": [],
                        "requires_confirmation": True,
                    }
                )
                break
        if len(case["plans"]) >= 4:
            break
    case["plans"] = case["plans"][:4]
    return case


def window_question(task, window):
    members = [i for i in task["intents"] if i["intent_id"] in window["intent_ids"]]
    need_duration = [
        i["event"].get("title", i["intent_id"])
        for i in members
        if not i.get("duration_minutes") and not all(i["event"].get(k) for k in ("start", "end"))
    ]
    related = [
        r
        for r in task["relations"]
        if r.get("confirmed")
        and r["first"] in window["intent_ids"]
        and r["second"] in window["intent_ids"]
    ]
    prefix = ("、".join(need_duration) + "各需要多久？") if need_duration else "已记下各项时长。"
    if any(r.get("kind") == "parallel" for r in related):
        return prefix + "已记录同时进行，请补充尚未确定的时间分配，并核实往返与注意力能否兼顾。"
    if any(r.get("kind") == "sequential" for r in related):
        return prefix + "已记录先后顺序，请补充尚未确定的时间分配或分界。"
    return prefix + "先做哪项？请说明时间分配。"


def clarification_view(task):
    if not task or task["expires"] <= time.time():
        return None
    indexed = {i["intent_id"]: i for i in task["intents"]}
    windows = []
    for window in task.get("time_windows", []):
        if any(
            not indexed[i]["event"].get(k) for i in window["intent_ids"] for k in ("start", "end")
        ):
            windows.append(
                {
                    "start": window["start"],
                    "end": window["end"],
                    "titles": [indexed[i]["event"].get("title", i) for i in window["intent_ids"]],
                    "question": window_question(task, window),
                }
            )
    return {"windows": windows} if windows else None


def stage(toolset, value):
    store, cal = toolset.store, toolset.cal
    previous = toolset.state.get("task")
    if previous and previous["expires"] <= time.time():
        raise CalendarError("task_expired", hint="草稿过期，请新建对话重新描述")
    task = (
        copy.deepcopy(previous)
        if previous
        else {"id": uuid4().hex, "expires": time.time() + 900, "intents": [], "relations": []}
    )
    task["revision"] = task.get("revision", 0) + 1
    if (
        not previous
        and toolset.state.get("conflict")
        and toolset.state["conflict"].get("kind") != "task"
    ):
        task["source_context"] = copy.deepcopy(toolset.state["conflict"])
    incoming = [i.intent_id for i in value.intents]
    if len(set(incoming)) != len(incoming):
        raise CalendarError("duplicate_intent", hint="每条事件使用不同intent_id")
    by_id = {i["intent_id"]: i for i in task["intents"]}
    observed = toolset.state.get("candidates", [])
    for raw in value.model_dump(exclude_none=True)["intents"]:
        identifier = raw["intent_id"]
        old = by_id.get(identifier)
        if old:
            if raw.get("action", old["action"]) != old["action"] or any(
                k in raw and raw[k] != old.get(k) for k in ("event_id", "version")
            ):
                raise CalendarError("invalid_intent", hint="不能复用intent_id替换另一条目标")
            patch = raw.get("event", {})
            old["event"].update(patch)
        else:
            action = raw.get("action", "create")
            base = {}
            if action == "update":
                found = next(
                    (
                        e
                        for e in observed
                        if e["id"] == raw.get("event_id") and e["version"] == raw.get("version")
                    ),
                    None,
                )
                if not found:
                    raise CalendarError("not_observed")
                base = {k: found[k] for k in EventInput.model_fields}
            elif raw.get("event_id") is not None or raw.get("version") is not None:
                raise CalendarError("invalid_intent")
            item = {"intent_id": identifier, "action": action, "event": base | raw.get("event", {})}
            if action == "update":
                item.update(event_id=raw["event_id"], version=raw["version"])
            task["intents"].append(item)
            by_id[identifier] = item
    if not 1 <= len(task["intents"]) <= 8:
        raise CalendarError("invalid_task")
    relations = {(r["first"], r["second"]): r for r in task["relations"]}
    for raw in value.model_dump(exclude_none=True)["relations"]:
        if raw["first"] == raw["second"] or raw["first"] not in by_id or raw["second"] not in by_id:
            raise CalendarError("invalid_relation")
        key = (raw["first"], raw["second"])
        relations[key] = {**relations.get(key, {}), **raw}
    task["relations"] = list(relations.values())
    windows = {w["window_id"]: w for w in task.get("time_windows", [])}
    for raw in value.model_dump(exclude_none=True)["time_windows"]:
        ids = raw["intent_ids"]
        if len(set(ids)) != len(ids) or any(identifier not in by_id for identifier in ids):
            raise CalendarError("invalid_window")
        windows[raw["window_id"]] = {
            **raw,
            **{k: cal.iso(cal.timestamp(raw[k])) for k in ("start", "end")},
        }
    if len(windows) > 8:
        raise CalendarError("invalid_window")
    if windows:
        task["time_windows"] = list(windows.values())
    for raw in value.model_dump(exclude_none=True)["intents"]:
        item = by_id[raw["intent_id"]]
        if "duration_minutes" in raw:
            item["duration_minutes"] = raw["duration_minutes"]
        data = item["event"]
        if item.get("duration_minutes") and data.get("start"):
            end = cal.iso(cal.timestamp(data["start"]) + item["duration_minutes"] * 60)
            if raw.get("event", {}).get("end"):
                if cal.timestamp(data["end"]) != cal.timestamp(end):
                    raise CalendarError(
                        "inconsistent_duration", hint="结束时间与明确时长不一致，请核实"
                    )
            elif (
                "duration_minutes" in raw or "start" in raw.get("event", {}) or not data.get("end")
            ):
                data["end"] = end
    if previous:
        previous_by_id = {i["intent_id"]: i for i in previous["intents"]}
        for item in task["intents"]:
            before = previous_by_id.get(item["intent_id"], {}).get("event", {})
            if all(before.get(k) and item["event"].get(k) for k in ("start", "end")):
                cal.check_constraints(before, item["event"])
    validate_windows(cal, task, {i["intent_id"]: i["event"] for i in task["intents"]})
    for intent in task["intents"]:
        for field in ("start", "end"):
            if intent["event"].get(field):
                cal.timestamp(intent["event"][field])
        if all(intent["event"].get(k) for k in ("start", "end")):
            if cal.timestamp(intent["event"]["end"]) <= cal.timestamp(intent["event"]["start"]):
                raise CalendarError("invalid_duration")
    toolset.state.pop("conflict", None)
    toolset.state["task"] = task
    store.invalidate(toolset.sid)
    toolset._save_state()
    missing = []
    shared_incomplete = set()
    by_id = {i["intent_id"]: i for i in task["intents"]}
    for window in task.get("time_windows", []):
        members = [by_id[i] for i in window["intent_ids"]]
        if not any(not m["event"].get(k) for m in members for k in ("start", "end")):
            continue
        shared_incomplete.update(window["intent_ids"])
        names = "、".join(m["event"].get("title", m["intent_id"]) for m in members)
        known = "；".join(
            f"{m['event'].get('title', m['intent_id'])}已知"
            + "、".join(
                f"{label}{m['event'][k]}"
                for k, label in (("start", "开始"), ("end", "结束"))
                if m["event"].get(k)
            )
            for m in members
            if any(m["event"].get(k) for k in ("start", "end"))
        )
        start_label = window["start"][:16].replace("T", " ")
        end_label = window["end"][:16].replace("T", " ")
        missing.append(
            f"尚未添加。已记下{names}：{start_label}–{end_label}（{cal.tz}）{('；' + known) if known else ''}。"
            + window_question(task, window)
            + "补充后会生成待确认清单，点击确认才会添加；不会自动均分或假定并行。"
        )
    for item in task["intents"]:
        data = item["event"]
        fields = [
            label
            for field, label in [
                ("title", "标题"),
                ("start", "开始时间"),
                ("end", "结束时间或持续时间"),
            ]
            if not data.get(field)
            and (field == "title" or item["intent_id"] not in shared_incomplete)
        ]
        if fields:
            missing.append(f"{data.get('title', item['intent_id'])}：请补充{'、'.join(fields)}。")
        elif all(data.get(k) for k in ("title", "start", "end")):
            item["event"] = normalized(cal, data)
    toolset._save_state()
    if missing:
        return {"question": "\n".join(missing), "task": task}
    # Compile relationship permissions onto a copy; later clarification/revocation
    # starts from the canonical per-event draft, never from derived authorization.
    task = copy.deepcopy(task)
    by_id = {i["intent_id"]: i for i in task["intents"]}
    # Pair-specific server token, only after explicit interruption/travel/attention consent.
    questions = []
    for relation in task["relations"]:
        if relation.get("kind") not in ("interrupt", "parallel") or not relation.get("confirmed"):
            continue
        a, b = by_id[relation["first"]]["event"], by_id[relation["second"]]["event"]
        if not relation.get("travel_included") or not relation.get("attention_verified"):
            questions.append(
                (
                    f"{a['title']}、{b['title']}是否都占用各自所填的完整区间，请核对各项起止时间；"
                    if relation["kind"] == "parallel"
                    else ""
                )
                + f"{b['title']}的时间包含往返吗，期间注意力能否兼顾？"
            )
            continue
        if any(e["interruptibility"] == "noninterruptible" for e in (a, b)):
            continue
        token = task["id"] + ":" + relation["first"] + ":" + relation["second"]
        for e in (a, b):
            e.update(overlap_allowed=True, coexistence_verified=True)
            e["coexistence_groups"] = list(set(e.get("coexistence_groups", [])) | {token})
        if relation["kind"] == "interrupt":
            a["interruptibility"] = "pausable"
        else:
            a["interruptibility"] = "parallel"
    toolset._save_state()
    changes = changes_for(task)
    try:
        validate_task(store, task, changes)
        cal.prepare_batch(changes)
    except CalendarError as exc:
        if exc.code not in ("conflict", "sequence_conflict"):
            raise
        case = build_case(store, task, changes)
        toolset.state["conflict"] = case
        toolset._save_state()
        question = (
            "\n".join(questions)
            if questions
            else (
                "这些安排有重叠。可以短暂暂停其中一项吗？"
                if any(f["classification"] == "needs_clarification" for f in case["facts"])
                else "这些安排需要错开，请选择方案或说明可调整的时间。"
            )
        )
        return {"question": question, "conflict": case, "task": task}
    return {
        "proposal": store.propose(
            toolset.sid, "batch", {"changes": changes, "task": copy.deepcopy(task)}
        )
    }

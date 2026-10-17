"""Range review and recoverable operations atop the existing proposal transaction."""

import copy
import json
import time
import uuid
from datetime import datetime, timedelta

from scheduler.calendar import CalendarError, EventInput, RETENTION_SECONDS


def resolve_range(cal, scope, now=None):
    now = now or datetime.now(cal.tz)
    now = now.astimezone(cal.tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    kind = scope["kind"]
    if kind == "today":
        start, end = midnight, midnight + timedelta(days=1)
    elif kind == "tomorrow":
        start, end = midnight + timedelta(days=1), midnight + timedelta(days=2)
    elif kind == "week":
        start = midnight - timedelta(days=midnight.weekday())
        end = start + timedelta(days=7)
    elif kind == "afternoon":
        start, end = midnight.replace(hour=12), midnight.replace(hour=18)
    elif kind == "date":
        try:
            start = datetime.fromisoformat(scope["date"]).replace(tzinfo=cal.tz)
        except (KeyError, ValueError, TypeError) as exc:
            raise CalendarError("invalid_range") from exc
        if len(scope["date"]) != 10:
            raise CalendarError("invalid_range")
        end = start + timedelta(days=1)
    elif kind == "interval":
        if not scope.get("start") or not scope.get("end"):
            raise CalendarError("invalid_range")
        a, b = cal.timestamp(scope["start"]), cal.timestamp(scope["end"])
        start, end = datetime.fromtimestamp(a, cal.tz), datetime.fromtimestamp(b, cal.tz)
    else:
        raise CalendarError("invalid_range")
    a, b = cal.timestamp(start.isoformat()), cal.timestamp(end.isoformat())
    if b <= a or b - a > 366 * 86400:
        raise CalendarError("invalid_range")
    return {"start": cal.iso(a), "end": cal.iso(b)}


def scope_events(cal, scope, conn=None):
    if conn is None:
        with cal.connect() as db:
            return scope_events(cal, scope, db)
    a, b = cal.timestamp(scope["start"]), cal.timestamp(scope["end"])
    return [
        cal.serialize(r)
        for r in conn.execute(
            "SELECT * FROM events WHERE deleted_at IS NULL AND start<? AND end>? ORDER BY start,id",
            (b, a),
        )
    ]


def validate_cancel(store, payload, conn=None):
    current = scope_events(store.calendar, payload["range"], conn)
    expected = payload["snapshot"]
    if {(e["id"], e["version"]) for e in current} != {(e["id"], e["version"]) for e in expected}:
        raise CalendarError("stale_batch", hint="范围内日程已变化，请重新查看列表并确认")
    allowed = {e["id"] for e in expected}
    ids = [e["id"] for e in payload["selected"]]
    if (
        not ids
        or len(set(ids)) != len(ids)
        or not set(ids) <= allowed
        or payload.get("unresolved_exclusion")
    ):
        raise CalendarError("invalid_selection", hint="请先明确勾选需要取消的日程")
    if any(
        e["version"] != next(x["version"] for x in expected if x["id"] == e["id"])
        for e in payload["selected"]
    ):
        raise CalendarError("stale_batch")
    return current


def propose_cancel(
    store, sid, scope=None, *, now=None, exclude_ids=(), exclude_queries=(), continue_previous=False
):
    state = store.session(sid)["state"]
    if continue_previous:
        review = state.get("cancel_review")
        if not review or review["expires"] <= time.time():
            raise CalendarError("batch_expired", hint="取消清单已过期，请重新指定范围")
        payload = copy.deepcopy(review["payload"])
        # Revalidate even when the unresolved exclusion prevented prior confirmation.
        current = scope_events(store.calendar, payload["range"])
        if {(e["id"], e["version"]) for e in current} != {
            (e["id"], e["version"]) for e in payload["snapshot"]
        }:
            raise CalendarError("stale_batch", hint="日程已变化，请重新指定范围")
    else:
        resolved = resolve_range(store.calendar, scope, now)
        events = scope_events(store.calendar, resolved)
        if not events:
            store.invalidate(sid)
            state.pop("cancel_review", None)
            store.save_state(sid, state)
            return None
        if len(events) > 1000:
            raise CalendarError("batch_too_large", hint="范围超过1000项，请缩小范围；没有截断执行")
        payload = {"range": resolved, "snapshot": events, "selected": events, "excluded_ids": []}
    allowed = {e["id"] for e in payload["snapshot"]}
    if not set(exclude_ids) <= allowed:
        raise CalendarError("not_observed")
    excluded = set(payload["excluded_ids"]) | set(exclude_ids)
    unresolved = list(payload.get("unresolved_exclusion", []))
    explicitly_resolved = set(exclude_ids)
    for query in exclude_queries:
        matches = [
            e
            for e in payload["snapshot"]
            if query.strip() and query.casefold() in e["title"].casefold()
        ]
        if len(matches) != 1:
            if query not in unresolved:
                unresolved.append(query)
        else:
            excluded.add(matches[0]["id"])
            explicitly_resolved.add(matches[0]["id"])
    unresolved = [
        query
        for query in unresolved
        if not any(
            e["id"] in explicitly_resolved
            and query.strip()
            and query.casefold() in e["title"].casefold()
            for e in payload["snapshot"]
        )
    ]
    payload["excluded_ids"] = [e["id"] for e in payload["snapshot"] if e["id"] in excluded]
    payload["selected"] = [e for e in payload["snapshot"] if e["id"] not in excluded]
    payload["unresolved_exclusion"] = unresolved
    if not payload["selected"]:
        store.invalidate(sid)
        state.pop("cancel_review", None)
        store.save_state(sid, state)
        return None
    proposal = store.propose(sid, "cancel_batch", payload)
    state.pop("task", None)
    state.pop("conflict", None)
    state["cancel_review"] = {
        "payload": payload,
        "expires": proposal["expires"],
        "proposal_id": proposal["id"],
    }
    store.save_state(sid, state)
    if unresolved:
        return {
            "question": "保留对象不唯一或未找到："
            + "、".join(unresolved)
            + "。请在清单中取消勾选要保留的具体日程。",
            "review": proposal,
        }
    return proposal


def revise_cancel(store, sid, proposal_id, selected_ids):
    pending = store.pending(sid)
    if not pending or pending["id"] != proposal_id or pending["action"] != "cancel_batch":
        raise CalendarError("proposal_expired")
    payload = copy.deepcopy(pending["event"])
    current = scope_events(store.calendar, payload["range"])
    if {(e["id"], e["version"]) for e in current} != {
        (e["id"], e["version"]) for e in payload["snapshot"]
    }:
        raise CalendarError("stale_batch")
    allowed = {e["id"] for e in payload["snapshot"]}
    if len(set(selected_ids)) != len(selected_ids) or not set(selected_ids) <= allowed:
        raise CalendarError("invalid_selection")
    if not selected_ids:
        raise CalendarError("invalid_selection", hint="至少选择一项；如需全部保留，请取消本次操作")
    payload["selected"] = [e for e in payload["snapshot"] if e["id"] in selected_ids]
    payload["excluded_ids"] = [e["id"] for e in payload["snapshot"] if e["id"] not in selected_ids]
    payload["unresolved_exclusion"] = []
    result = store.propose(sid, "cancel_batch", payload)
    state = store.session(sid)["state"]
    state["cancel_review"] = {
        "payload": payload,
        "proposal_id": result["id"],
        "expires": result["expires"],
    }
    store.save_state(sid, state)
    return result


def restore_facts(store, events, conn=None):
    cal = store.calendar
    ids = {e["id"] for e in events}
    facts = []
    feasible = []
    accepted = []
    for index, event in enumerate(events):
        blockers = [
            e for e in cal.conflicts(event["start"], event["end"], conn=conn) if e["id"] not in ids
        ]
        active_blockers = list(blockers)
        blockers += events[:index]
        for other in blockers:
            start = max(cal.timestamp(event["start"]), cal.timestamp(other["start"]))
            end = min(cal.timestamp(event["end"]), cal.timestamp(other["end"]))
            if start < end and not cal.compatible(event, other):
                facts.append(
                    {
                        "event": event,
                        "existing": other,
                        "overlap": {"start": cal.iso(start), "end": cal.iso(end)},
                    }
                )
        # Feasible subset is a separate greedy set, excluding already-blocked rows.
        if not any(
            cal.timestamp(event["start"]) < cal.timestamp(other["end"])
            and cal.timestamp(event["end"]) > cal.timestamp(other["start"])
            and not cal.compatible(event, other)
            for other in active_blockers + accepted
        ):
            feasible.append(event["id"])
            accepted.append(event)
    return facts, feasible


def validate_restore(store, payload, conn=None):
    cal = store.calendar
    current = []
    for snapshot in payload["selected"]:
        event = cal.trash_get(snapshot["id"], conn)
        if (
            not event
            or event["version"] != snapshot["version"]
            or event["deletion_batch_id"] != snapshot["deletion_batch_id"]
        ):
            raise CalendarError("stale_trash", hint="回收站记录已恢复、移除或发生变化，请刷新")
        if cal.timestamp(event["deleted_at"]) + RETENTION_SECONDS <= time.time():
            raise CalendarError("trash_expired", hint="已超过7天保留期，不能恢复")
        adjusted = event | payload.get("adjustments", {}).get(event["id"], {})
        cal.validate({k: adjusted[k] for k in EventInput.model_fields})
        cal.check_constraints(event, adjusted)
        if cal.timestamp(adjusted["end"]) - cal.timestamp(adjusted["start"]) != cal.timestamp(
            event["end"]
        ) - cal.timestamp(event["start"]):
            raise CalendarError("hard_constraint")
        current.append(adjusted)
    if not current or len({e["id"] for e in current}) != len(current):
        raise CalendarError("invalid_selection")
    facts, _ = restore_facts(store, current, conn)
    if facts:
        raise CalendarError(
            "restore_conflict",
            hint="恢复与现有日程冲突，未执行任何恢复，请选择明确的可行子集或取消",
            conflicts=facts,
        )
    return current


def propose_restore(store, sid, *, event_ids=(), operation_id=None, expected=None, is_undo=False):
    if operation_id:
        with store.calendar.connect() as conn:
            row = conn.execute(
                "SELECT result,action FROM proposals WHERE id=? AND session_id=? AND status='committed'",
                (operation_id, sid),
            ).fetchone()
        if not row or row["action"] not in ("delete", "cancel_batch"):
            raise CalendarError("operation_not_found")
        result = json.loads(row["result"])
        original_ids = result.get("event_ids") or [result["event"]["id"]]
        events = [
            e
            for target in original_ids
            if (e := store.calendar.trash_get(target)) and e["deletion_batch_id"] == operation_id
        ]
    else:
        events = [store.calendar.trash_get(target) for target in event_ids]
    if not events or any(e is None for e in events):
        raise CalendarError("trash_not_found", hint="该批记录已恢复或不可用")
    if len(events) > 1000 or len({e["id"] for e in events}) != len(events):
        raise CalendarError("invalid_selection")
    if expected is not None and {
        (e["id"], e["version"], e["deletion_batch_id"]) for e in events
    } != {(e["id"], e["version"], e["deletion_batch_id"]) for e in expected}:
        raise CalendarError("stale_trash")
    for e in events:
        if store.calendar.timestamp(e["deleted_at"]) + RETENTION_SECONDS <= time.time():
            raise CalendarError("trash_expired", hint="已超过7天保留期")
    facts, feasible = restore_facts(store, events)
    return store.propose(
        sid,
        "restore_batch",
        {
            "selected": events,
            "conflicts": facts,
            "feasible_ids": feasible,
            "alternatives": restore_alternatives(store, events) if facts else [],
            "source_operation_id": operation_id,
            "is_undo": is_undo,
        },
    )


def restore_alternatives(store, events):
    """Bounded deterministic whole-selection plans; never move active events."""
    cal = store.calendar
    # Avoid expensive combinatorial planning for a large batch. Explicit feasible
    # subsets remain available, and individual rows can be resolved separately.
    if len(events) > 20:
        return []
    plans = []
    for offset in (0, 60, 120):
        selected = []
        for original in events:
            candidate = dict(original)
            if restore_facts(store, selected + [candidate])[0]:
                duration = (cal.timestamp(original["end"]) - cal.timestamp(original["start"])) / 60
                if (
                    not 5 <= duration <= 1440
                    or original.get("keep_time")
                    or original.get("flexibility") in ("fixed", "shortenable")
                ):
                    break
                begin = cal.timestamp(original["start"]) + offset * 60
                slots = cal.slots(cal.iso(begin), cal.iso(begin + 2 * 86400), duration)
                for slot in slots:
                    proposed = candidate | slot
                    cal.check_constraints(original, proposed)
                    if not restore_facts(store, selected + [proposed])[0]:
                        candidate = proposed
                        break
                else:
                    break
            selected.append(candidate)
        if (
            len(selected) == len(events)
            and selected != events
            and not any(p["selected"] == selected for p in plans)
        ):
            plans.append({"id": uuid.uuid4().hex, "selected": selected})
    return plans


def choose_restore(store, sid, proposal_id, option_id):
    proposal = store.pending(sid)
    if not proposal or proposal["id"] != proposal_id or proposal["action"] != "restore_batch":
        raise CalendarError("stale_proposal", hint="方案已过期或失效，请从回收站重新选择")
    payload = copy.deepcopy(proposal["event"])
    if option_id == "original":
        # A new review, not permission to bypass a hard conflict.
        return propose_restore(
            store,
            sid,
            event_ids=[e["id"] for e in payload["selected"]],
            expected=payload["selected"],
        )
    option = next((o for o in payload.get("alternatives", []) if o["id"] == option_id), None)
    if not option:
        raise CalendarError("invalid_selection")
    payload["adjustments"] = {
        e["id"]: {k: e[k] for k in ("start", "end")} for e in option["selected"]
    }
    selected = validate_restore(store, payload)
    payload.update(
        selected=selected, conflicts=[], feasible_ids=[e["id"] for e in selected], alternatives=[]
    )
    return store.propose(sid, "restore_batch", payload)


def execute(store, sid, action, payload, operation_id, conn):
    cal = store.calendar
    if action == "cancel_batch":
        validate_cancel(store, payload, conn)
        events = payload["selected"]
        verified_tombstones = []
        for event in events:
            cal.delete(event["id"], event["version"], conn, batch_id=operation_id)
            verified_tombstones.append(cal.trash_get(event["id"], conn))
        for original, expected_tombstone in zip(events, verified_tombstones, strict=True):
            trashed = cal.trash_get(original["id"], conn)
            if (
                cal.get(original["id"], conn) is not None
                or not trashed
                or trashed != expected_tombstone
                or trashed["deletion_batch_id"] != operation_id
                or trashed["version"] != original["version"] + 1
                or any(trashed[k] != original[k] for k in EventInput.model_fields)
            ):
                raise CalendarError("verification_failed")
        return {
            "status": "committed",
            "action": action,
            "count": len(events),
            "operation_id": operation_id,
            "event_ids": [e["id"] for e in events],
            "message": f"已移至回收站 {len(events)} 项日程，7天内可恢复。",
        }
    events = validate_restore(store, payload, conn)
    for event in events:
        conn.execute(
            "UPDATE events SET start=?,end=?,deleted_at=NULL,deletion_batch_id=NULL,version=version+1 WHERE id=? AND version=? AND deletion_batch_id=?",
            (
                cal.timestamp(event["start"]),
                cal.timestamp(event["end"]),
                event["id"],
                event["version"],
                event["deletion_batch_id"],
            ),
        )
    for event in events:
        expected = {k: event[k] for k in EventInput.model_fields} | {
            "id": event["id"],
            "version": event["version"] + 1,
        }
        if cal.get(event["id"], conn) != expected or cal.trash_get(event["id"], conn) is not None:
            raise CalendarError("verification_failed")
    return {
        "status": "committed",
        "action": action,
        "count": len(events),
        "operation_id": operation_id,
        "event_ids": [e["id"] for e in events],
        "message": f"已恢复并核验 {len(events)} 项日程。",
    }


def undo(store, sid, operation_id):
    # Explicit UI click authorizes this exact operation. The LLM can only propose.
    with store.calendar.connect() as conn:
        rows = conn.execute(
            "SELECT payload,result FROM proposals WHERE session_id=? AND action='restore_batch' AND status='committed' ORDER BY rowid DESC",
            (sid,),
        ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("is_undo") and payload.get("source_operation_id") == operation_id:
            return json.loads(row["result"])
    proposal = propose_restore(store, sid, operation_id=operation_id, is_undo=True)
    if proposal["event"]["conflicts"]:
        return {"status": "proposal", "proposal": proposal}
    return store.confirm(sid, proposal["id"], True)

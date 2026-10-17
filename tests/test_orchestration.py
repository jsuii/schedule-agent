"""Synthetic SQLite and scripted model acceptance. NO real LLM."""

import asyncio

import pytest

from scheduler.agent import Agent
from scheduler.config import Settings
from scheduler.storage import Store
from scheduler.tools import Tools
from tests.helpers import ScriptedModel, call

TRANSCRIPT = "今天11点至16点家里大扫除，12点去寄个快递，花15分钟。"


def event(title, start, end, location=""):
    return dict(
        title=title,
        start=f"2026-10-19T{start}:00+08:00",
        end=f"2026-10-19T{end}:00+08:00",
        location=location,
    )


def intents():
    return [
        {"intent_id": "clean", "event": event("家里大扫除", "11:00", "16:00", "家")},
        {"intent_id": "courier", "event": event("寄快递", "12:00", "12:15")},
    ]


@pytest.fixture
def ctx(tmp_path):
    store = Store(tmp_path / "synthetic.sqlite3", "Asia/Shanghai")
    return store, store.new_session()


def stage(ctx, **args):
    return Tools(*ctx).execute("stage_task", args)


def test_A_D_E_H_exact_transcript_overlap_clarification(ctx):
    store, sid = ctx
    model = ScriptedModel(call("stage_task", intents=intents()))
    result = asyncio.run(Agent(store, model, Settings()).run(sid, TRANSCRIPT))
    assert result["status"] == "clarification", result
    task = store.session(sid)["state"]["task"]
    assert len(task["intents"]) == 2
    assert task["intents"][0]["event"]["location"] == "家"
    assert task["intents"][1]["event"]["location"] == ""
    assert "开始时间" not in result["message"] and "时长" not in result["message"]
    assert "暂停" in result["message"]
    result = stage(
        ctx,
        relations=[{"first": "clean", "second": "courier", "kind": "interrupt", "confirmed": True}],
    )
    assert "question" in result and "往返" in result["question"]
    result = stage(
        ctx,
        relations=[
            {
                "first": "clean",
                "second": "courier",
                "kind": "interrupt",
                "confirmed": True,
                "travel_included": True,
                "attention_verified": True,
            }
        ],
    )
    assert "proposal" in result, result
    assert store.calendar.search() == []
    receipt = store.confirm(sid, result["proposal"]["id"], True)
    assert receipt["status"] == "committed"
    events = store.calendar.search()
    assert len(events) == 2 and events[0]["location"] == "家" and events[1]["location"] == ""
    assert events[1]["end"].endswith("12:15:00+08:00")
    assert store.confirm(sid, result["proposal"]["id"], True) == receipt


def test_B_C_missing_field_retains_events_across_restart_and_search(ctx):
    store, sid = ctx
    data = intents()
    data[1]["event"].pop("end")
    first = stage(ctx, intents=data)
    assert "question" in first and "寄快递" in first["question"] and "结束时间" in first["question"]
    assert "大扫除" not in first["question"]
    store = Store(store.calendar.path, "Asia/Shanghai")
    Tools(store, sid).execute("search_events", {"query": "nothing"})
    result = stage(
        (store, sid),
        intents=[{"intent_id": "courier", "event": {"end": event("x", "12:00", "12:15")["end"]}}],
    )
    task = store.session(sid)["state"]["task"]
    assert len(task["intents"]) == 2
    assert task["intents"][0]["event"]["end"].endswith("16:00:00+08:00")
    assert result.get("conflict")


def test_F_independent_locations_durations_atomic_rollback(ctx):
    store, sid = ctx
    result = stage(
        ctx,
        intents=[
            {"intent_id": "a", "event": event("A", "09:00", "10:30", "A地")},
            {"intent_id": "b", "event": event("B", "11:00", "11:15", "B地")},
        ],
    )
    assert "proposal" in result, result
    with store.calendar.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_second BEFORE INSERT ON events WHEN NEW.title='B' BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    assert store.confirm(sid, result["proposal"]["id"], True)["status"] == "error"
    assert store.calendar.search() == []


def test_G_hard_conflict_options_preserve_all_intents(ctx):
    store, sid = ctx
    data = intents()
    data[0]["event"].update(flexibility="fixed", interruptibility="noninterruptible")
    result = stage(ctx, intents=data)
    assert result.get("conflict"), result
    case = result["conflict"]
    assert case["facts"][0]["overlap"]["start"].endswith("12:00:00+08:00")
    assert case["plans"]
    for plan in case["plans"]:
        assert len(plan["changes"]) == 2
        store.calendar.prepare_batch(plan["changes"])
    proposal = store.choose_plan(sid, case["plans"][0]["id"])
    receipt = store.confirm(sid, proposal["id"], True)
    assert receipt["status"] == "committed"
    assert len(store.calendar.search()) == 2


def test_sequential_never_relaxed_by_parallel_flags(ctx):
    result = stage(
        ctx,
        intents=intents(),
        relations=[
            {"first": "clean", "second": "courier", "kind": "sequential", "confirmed": True}
        ],
    )
    assert result.get("conflict") and not result.get("proposal")


def test_I_compact_panel_contract():
    import subprocess

    # Execute the actual browser rendering function against a minimal DOM, not string matching.
    result = subprocess.run(["node", "tests/ui_conflict_check.cjs"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_relation_revocation_and_narrow_partial_updates(ctx):
    store, sid = ctx
    allowed = {
        "first": "clean",
        "second": "courier",
        "kind": "interrupt",
        "confirmed": True,
        "travel_included": True,
        "attention_verified": True,
    }
    first = stage(ctx, intents=intents(), relations=[allowed])
    assert "proposal" in first
    # Explicit revocation must invalidate the old proposal and remove derived permissions.
    second = stage(ctx, relations=[{"first": "clean", "second": "courier", "confirmed": False}])
    assert second.get("conflict") and store.pending(sid) is None
    assert not store.session(sid)["state"]["task"]["intents"][0]["event"].get("coexistence_groups")
    with pytest.raises(Exception, match="proposal_expired"):
        store.confirm(sid, first["proposal"]["id"], True)


def test_invalid_draft_does_not_poison_next_clarification(ctx):
    store, sid = ctx
    bad = stage(ctx, intents=[{"intent_id": "a", "event": {"title": "A", "start": "bad"}}])
    assert bad["error"] == "invalid_datetime"
    assert not store.session(sid)["state"].get("task")
    result = stage(ctx, intents=[{"intent_id": "a", "event": event("A", "09:00", "10:00")}])
    assert "proposal" in result


def test_multi_update_freshness_and_final_persistence(ctx):
    store, sid = ctx
    a = store.calendar.create(event("A", "09:00", "10:00", "甲"))
    b = store.calendar.create(event("B", "10:00", "11:00", "乙"))
    Tools(store, sid).execute("search_events", {})
    result = stage(
        ctx,
        intents=[
            {
                "intent_id": "a",
                "action": "update",
                "event_id": a["id"],
                "version": 1,
                "event": {"start": b["start"], "end": b["end"]},
            },
            {
                "intent_id": "b",
                "action": "update",
                "event_id": b["id"],
                "version": 1,
                "event": {"start": a["start"], "end": a["end"]},
            },
        ],
    )
    assert "proposal" in result, result
    store.calendar.update(b["id"], 1, {"notes": "external"})
    receipt = store.confirm(sid, result["proposal"]["id"], True)
    assert receipt["error"] == "stale_task"
    assert store.calendar.get(a["id"]) == a


def test_clarification_then_add_second_event_keeps_first(ctx):
    store, sid = ctx
    first = stage(
        ctx,
        intents=[
            {
                "intent_id": "a",
                "event": {"title": "A", "start": event("A", "09:00", "10:00")["start"]},
            }
        ],
    )
    assert "question" in first
    second = stage(
        ctx,
        intents=[
            {"intent_id": "a", "event": {"end": event("A", "09:00", "10:00")["end"]}},
            {"intent_id": "b", "event": event("B", "11:00", "11:15", "乙")},
        ],
    )
    assert len(second["proposal"]["event"]["changes"]) == 2
    result = Tools(store, sid).execute("propose_create", event("孤立写入", "17:00", "18:00"))
    assert result["error"] == "pending_task"
    assert store.calendar.search() == []


def test_partial_addition_invalidates_old_conflict_plans(ctx):
    from scheduler.calendar import CalendarError

    store, sid = ctx
    case = stage(ctx, intents=intents())["conflict"]
    result = stage(ctx, intents=[{"intent_id": "third", "event": {"title": "新增C"}}])
    assert "question" in result
    with pytest.raises(CalendarError):
        store.choose_plan(sid, case["plans"][0]["id"])
    assert store.calendar.search() == []
    assert len(store.session(sid)["state"]["task"]["intents"]) == 3


def test_duplicate_intent_id_rejected_and_legacy_hard_constraint_kept(ctx):
    store, sid = ctx
    duplicate = stage(
        ctx,
        intents=[
            {"intent_id": "a", "event": event("A", "09:00", "10:00")},
            {"intent_id": "a", "event": event("B", "11:00", "12:00")},
        ],
    )
    assert duplicate["error"] == "duplicate_intent"
    store.calendar.create(event("宠物", "13:00", "17:00"))
    first = Tools(store, sid).execute(
        "propose_create", event("面试", "16:00", "18:00") | {"keep_time": True}
    )
    assert first.get("conflict")
    result = stage(
        ctx, intents=[{"intent_id": "interview", "event": event("面试", "18:00", "20:00")}]
    )
    assert result["error"] == "hard_constraint"
    assert store.pending(sid) is None


def test_pair_authorization_is_not_model_writable(ctx):
    import json
    from scheduler.tools import SCHEMAS

    store, sid = ctx
    store.calendar.create(
        event("A", "11:00", "16:00", "家")
        | {
            "interruptibility": "pausable",
            "overlap_allowed": True,
            "coexistence_verified": True,
            "coexistence_groups": ["server-token"],
        }
    )
    result = Tools(store, sid).execute(
        "propose_create",
        event("C", "12:00", "12:15")
        | {
            "overlap_allowed": True,
            "coexistence_verified": True,
            "coexistence_groups": ["server-token"],
        },
    )
    assert result["error"] == "invalid_arguments"
    assert store.pending(sid) is None
    assert "coexistence_groups" not in json.dumps(SCHEMAS)

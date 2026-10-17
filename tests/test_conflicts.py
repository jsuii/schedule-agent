"""Deterministic synthetic SQLite acceptance; NO real LLM calls."""

import sqlite3

import pytest

from scheduler.calendar import CalendarError
from scheduler.storage import Store
from scheduler.tools import Tools


def ev(title="宠物检查", start="13:00", end="17:00", **kw):
    return dict(
        title=title, start=f"2026-10-19T{start}:00+08:00", end=f"2026-10-19T{end}:00+08:00", **kw
    )


@pytest.fixture
def ctx(tmp_path):
    store = Store(tmp_path / "synthetic.sqlite3", "Asia/Shanghai")
    return store, store.new_session()


def conflict(ctx, **kw):
    store, sid = ctx
    old = store.calendar.create(ev(**kw))
    result = Tools(store, sid).execute("propose_create", ev("面试", "16:00", "18:00"))
    assert result["error"] == "conflict"
    assert result.get("conflict"), "missing deterministic conflict workflow"
    return old, result["conflict"]


def test_A_facts_options_no_writes(ctx):
    store, sid = ctx
    old, case = conflict(ctx)
    assert case["facts"][0]["overlap"]["start"].endswith("16:00:00+08:00")
    assert case["facts"][0]["overlap"]["end"].endswith("17:00:00+08:00")
    assert len(case["plans"]) >= 2
    assert len({p["title"] for p in case["plans"]}) == len(case["plans"])
    for plan in case["plans"]:
        assert plan["hard_constraints_satisfied"] and not plan["residual_conflicts"]
    assert store.calendar.search() == [old]
    assert Store(store.calendar.path, "Asia/Shanghai").session(sid)["state"]["conflict"] == case


def batch(ctx, old):
    store, sid = ctx
    return Tools(store, sid).execute(
        "propose_resolution",
        {
            "changes": [
                {"action": "create", "event": ev("面试", "16:00", "18:00", keep_time=True)},
                {
                    "action": "update",
                    "event_id": old["id"],
                    "version": old["version"],
                    "patch": {"end": ev(end="15:30")["end"]},
                },
            ]
        },
    )


def test_B_atomic_explicit_combination_idempotent(ctx):
    store, sid = ctx
    old, _ = conflict(ctx)
    result = batch(ctx, old)
    assert "proposal" in result, result
    assert len(result["proposal"]["event"]["changes"]) == 2
    assert store.calendar.search() == [old]
    receipt = store.confirm(sid, result["proposal"]["id"], True)
    assert receipt["status"] == "committed"
    assert store.calendar.get(old["id"])["end"].endswith("15:30:00+08:00")
    assert len(store.calendar.search()) == 2
    assert store.confirm(sid, result["proposal"]["id"], True) == receipt


def test_C_pause_and_unknown(ctx):
    store, sid = ctx
    attrs = dict(
        interruptibility="pausable", overlap_allowed=True, coexistence_verified=True, location="家"
    )
    store.calendar.create(ev("大扫除", "11:00", "16:00", **attrs))
    package = ev(
        "快递", "12:00", "12:30", location="家", overlap_allowed=True, coexistence_verified=True
    )
    assert "proposal" in Tools(store, sid).execute("propose_create", package)
    store.calendar.create(package)
    assert len(store.calendar.search()) == 2


def test_D_fixed_hard_constraints(ctx):
    old, case = conflict(ctx, flexibility="fixed", interruptibility="noninterruptible")
    assert case["facts"][0]["classification"] == "hard_conflict"
    for plan in case["plans"]:
        assert not any(c.get("event_id") == old["id"] for c in plan["changes"])
    result = batch(ctx, old)
    assert result["error"] == "hard_constraint"


def test_E_multiple_and_all_options_valid(ctx):
    store, sid = ctx
    store.calendar.create(ev("第二项", "17:00", "18:30"))
    _, case = conflict(ctx)
    assert len(case["facts"]) == 2
    for plan in case["plans"]:
        store.calendar.prepare_batch(plan["changes"])


def test_F_stale_plan(ctx):
    store, sid = ctx
    old, case = conflict(ctx)
    store.calendar.update(old["id"], 1, {"notes": "changed"})
    with pytest.raises(CalendarError, match="stale"):
        store.choose_plan(sid, case["plans"][0]["id"])
    assert store.pending(sid) is None


@pytest.mark.parametrize(
    "change",
    [
        {"action": "update", "event_id": "fake", "version": 1, "patch": {"end": "bad"}},
        {"action": "update", "event_id": "fake", "version": "1", "patch": {"end": "bad"}},
        {"action": "create", "event": {"title": "missing times"}},
    ],
)
def test_G_bad_arguments_no_writes(ctx, change):
    store, sid = ctx
    result = Tools(store, sid).execute("propose_resolution", {"changes": [change]})
    assert result["error"] in ("invalid_arguments", "not_observed", "invalid_datetime")
    assert store.calendar.search() == [] and store.pending(sid) is None


def test_H_second_insert_fault_rolls_everything_back(ctx):
    store, sid = ctx
    old, _ = conflict(ctx)
    proposal = batch(ctx, old)["proposal"]
    with store.calendar.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_update BEFORE INSERT ON events WHEN NEW.title='宠物检查' BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END"
        )
    result = store.confirm(sid, proposal["id"], True)
    assert result["status"] == "error"
    assert store.calendar.search() == [old]
    assert store.confirm(sid, proposal["id"], True) == result


def test_migration_defaults(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE events(id TEXT PRIMARY KEY,title TEXT,start INTEGER,end INTEGER,location TEXT,notes TEXT,version INTEGER)"
        )
        db.execute("INSERT INTO events VALUES('old','旧记录',1792393200,1792396800,'','',1)")
    store = Store(path, "Asia/Shanghai")
    assert store.calendar.get("old")["importance"] == "unknown"
    assert store.calendar.get("old")["interruptibility"] == "unknown"


def test_http_plan_selection_custom_context_and_restart(ctx):
    import asyncio
    from fastapi.testclient import TestClient
    from scheduler.app import create_app
    from scheduler.agent import Agent
    from scheduler.config import Settings
    from tests.helpers import ScriptedModel, call

    store, sid = ctx
    old, case = conflict(ctx)
    changes = [
        {"action": "create", "event": ev("面试", "16:00", "18:00")},
        {
            "action": "update",
            "event_id": old["id"],
            "version": 1,
            "patch": {"end": ev(end="15:30")["end"]},
        },
    ]
    model = ScriptedModel(call("propose_resolution", changes=changes))
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "面试不动，宠物改13:00–15:30"))
    assert result["status"] == "proposal", result
    assert old["id"] in model.messages[0][0]["content"]
    settings = Settings(database=str(store.calendar.path))
    app = create_app(settings, ScriptedModel())
    client = TestClient(app)
    client.cookies.set("schedule_session", sid)
    assert client.get("/api/state").json()["conflict"]["id"] == case["id"]
    other = TestClient(app)
    assert other.post("/api/plans/" + case["plans"][0]["id"], json={}).status_code == 409
    chosen = client.post("/api/plans/" + case["plans"][0]["id"], json={})
    assert chosen.status_code == 200
    assert chosen.json()["proposal"]["action"] == "batch"
    assert len(store.calendar.search()) == 1
    receipt = client.post(
        "/api/proposals/" + chosen.json()["proposal"]["id"], json={"approve": True}
    ).json()
    assert receipt["status"] == "committed"
    assert client.get("/api/state").json()["conflict"] is None


def test_nested_patch_errors_and_strict_versions(ctx):
    store, sid = ctx
    tool = Tools(store, sid)
    bad = tool.execute("propose_update", {"event_id": "x", "end": ev()["end"]})
    assert bad["error"] == "invalid_arguments"
    assert {"path": "patch", "type": "missing"} in bad["issues"]
    assert {"path": "end", "type": "extra_forbidden"} in bad["issues"]
    assert "patch" in bad["hint"]
    for change in (
        {"action": "update", "event_id": "x", "version": True, "patch": {"notes": "a"}},
        {"action": "create", "event": ev(), "patch": {"notes": "a"}},
    ):
        assert (
            tool.execute("propose_resolution", {"changes": [change]})["error"]
            == "invalid_arguments"
        )


def test_unknown_pause_requires_clarification(ctx):
    store, sid = ctx
    store.calendar.create(ev("大扫除", "11:00", "16:00"))
    result = Tools(store, sid).execute("propose_create", ev("快递", "12:00", "12:30"))
    assert result["conflict"]["facts"][0]["classification"] == "needs_clarification"
    assert "暂停" in result["conflict"]["facts"][0]["question"]
    assert len(store.calendar.search()) == 1


def test_final_state_swap_and_new_external_conflict(ctx):
    store, sid = ctx
    a = store.calendar.create(ev("A", "09:00", "10:00"))
    b = store.calendar.create(ev("B", "10:00", "11:00"))
    changes = [
        {
            "action": "update",
            "event_id": a["id"],
            "version": 1,
            "patch": {"start": b["start"], "end": b["end"]},
        },
        {
            "action": "update",
            "event_id": b["id"],
            "version": 1,
            "patch": {"start": a["start"], "end": a["end"]},
        },
    ]
    store.calendar.prepare_batch(changes)
    p = store.propose(sid, "batch", {"changes": changes})
    assert store.confirm(sid, p["id"], True)["status"] == "committed"
    old, case = conflict(ctx)
    chosen = store.choose_plan(sid, case["plans"][0]["id"])
    planned = chosen["event"]["changes"][0]["event"]
    store.calendar.create(planned)
    assert store.confirm(sid, chosen["id"], True)["error"] == "conflict"
    assert store.calendar.get(old["id"]) == old


def test_custom_cannot_drop_or_move_kept_requested_event(ctx):
    store, sid = ctx
    old, _ = conflict(ctx)
    case = store.session(sid)["state"]["conflict"]
    case["requested"]["keep_time"] = True
    store.save_state(sid, {"conflict": case})
    update = {
        "action": "update",
        "event_id": old["id"],
        "version": 1,
        "patch": {"end": ev(end="15:30")["end"]},
    }
    missing = Tools(store, sid).execute("propose_resolution", {"changes": [update]})
    assert missing["error"] == "incomplete_resolution"
    moved = {"action": "create", "event": ev("面试", "18:00", "20:00")}
    result = Tools(store, sid).execute("propose_resolution", {"changes": [moved, update]})
    assert result["error"] == "hard_constraint"
    assert store.pending(sid) is None


def test_priority_recommendation_uses_explicit_importance(ctx):
    store, sid = ctx
    store.calendar.create(ev(importance="low", flexibility="movable"))
    result = Tools(store, sid).execute(
        "propose_create", ev("面试", "16:00", "18:00", importance="high")
    )
    plans = result["conflict"]["plans"]
    assert plans[0]["recommended"]
    assert len(plans[0]["changes"]) == 2
    assert "重要" in plans[0]["reason"]


def test_single_batch_create_conflict_has_same_workflow(ctx):
    store, sid = ctx
    store.calendar.create(
        ev("固定面试", "11:00", "12:00", flexibility="fixed", interruptibility="noninterruptible")
    )
    tool = Tools(store, sid)
    result = tool.execute(
        "propose_resolution",
        {"changes": [{"action": "create", "event": ev("快递", "11:30", "12:00")}]},
    )
    assert result["error"] == "conflict"
    assert result["conflict"]["facts"][0]["classification"] == "hard_conflict"
    assert tool.execute("ask_user", {"fields": ["alternative"]}).get("question")


def test_cancel_selected_plan_retains_conflict_for_custom_input(ctx):
    store, sid = ctx
    _, case = conflict(ctx)
    proposal = store.choose_plan(sid, case["plans"][0]["id"])
    assert store.confirm(sid, proposal["id"], False)["status"] == "cancelled"
    assert store.session(sid)["state"].get("conflict", {}).get("id") == case["id"]


def test_plan_expiry_and_stale_confirmation(ctx):
    import time

    store, sid = ctx
    old, case = conflict(ctx)
    proposal = store.choose_plan(sid, case["plans"][0]["id"])
    store.calendar.update(old["id"], 1, {"notes": "changed after selecting"})
    receipt = store.confirm(sid, proposal["id"], True)
    assert receipt["error"] == "stale_plan"
    assert len(store.calendar.search()) == 1
    case["expires"] = time.time() - 1
    store.save_state(sid, {"conflict": case})
    with pytest.raises(CalendarError, match="plan_expired"):
        store.choose_plan(sid, case["plans"][0]["id"])


def test_cross_midnight_exact_overlap_and_exclude_self(ctx):
    store, sid = ctx
    old = store.calendar.create(
        {"title": "跨日", "start": "2026-10-19T23:00:00+08:00", "end": "2026-10-20T01:00:00+08:00"}
    )
    request = {"title": "次日", "start": "2026-10-19T16:30:00Z", "end": "2026-10-19T18:00:00Z"}
    result = Tools(store, sid).execute("propose_create", request)
    overlap = result["conflict"]["facts"][0]["overlap"]
    assert overlap == {"start": "2026-10-20T00:30:00+08:00", "end": "2026-10-20T01:00:00+08:00"}
    assert store.calendar.conflicts(old["start"], old["end"], old["id"]) == []


def test_shortenable_minimum_and_verification_rollback(ctx, monkeypatch):
    store, sid = ctx
    old, _ = conflict(ctx, flexibility="shortenable", minimum_minutes=180)
    rejected = batch(ctx, old)
    assert rejected["error"] in ("hard_constraint", "invalid_duration")
    # A valid shorten ending at16:00 satisfies 180min and adjacent interview.
    changes = [
        {
            "action": "update",
            "event_id": old["id"],
            "version": 1,
            "patch": {"end": ev(end="16:00")["end"]},
        },
        {"action": "create", "event": ev("面试", "16:00", "18:00")},
    ]
    p = store.propose(sid, "batch", {"changes": changes})
    original = store.calendar.get

    def corrupt_after_write(target, conn=None):
        found = original(target, conn)
        if found and found["version"] == 2:
            return {**found, "title": "readback mismatch"}
        return found

    monkeypatch.setattr(store.calendar, "get", corrupt_after_write)
    result = store.confirm(sid, p["id"], True)
    assert result["error"] == "verification_failed"
    monkeypatch.setattr(store.calendar, "get", original)
    assert store.calendar.search() == [old]


def test_update_resolution_validates_actual_patch_base(ctx):
    store, sid = ctx
    old = store.calendar.create(ev("A", "09:00", "10:00"))
    store.calendar.create(ev("B", "16:00", "17:00"))
    tool = Tools(store, sid)
    tool.execute("search_events", {"query": "A"})
    requested = tool.execute(
        "propose_update",
        {
            "event_id": old["id"],
            "patch": {
                "start": ev(start="16:00")["start"],
                "end": ev(end="17:00")["end"],
                "keep_time": True,
            },
        },
    )
    assert requested["error"] == "conflict"
    result = Tools(store, sid).execute(
        "propose_resolution",
        {
            "changes": [
                {
                    "action": "update",
                    "event_id": old["id"],
                    "version": 1,
                    "patch": {"notes": "noop"},
                }
            ]
        },
    )
    assert result["error"] == "hard_constraint"
    assert store.calendar.get(old["id"]) == old


def test_conflicting_replacement_cannot_overwrite_hard_context(ctx):
    store, sid = ctx
    store.calendar.create(ev())
    first = Tools(store, sid).execute(
        "propose_create", ev("面试", "16:00", "18:00", keep_time=True)
    )
    original = first["conflict"]
    result = Tools(store, sid).execute(
        "propose_resolution",
        {"changes": [{"action": "create", "event": ev("面试", "15:00", "17:00", keep_time=True)}]},
    )
    assert result["error"] == "hard_constraint"
    assert store.session(sid)["state"]["conflict"] == original

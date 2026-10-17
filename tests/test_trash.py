"""Synthetic SQLite/HTTP and scripted model tests; NO real LLM."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scheduler.calendar import CalendarError
from scheduler.storage import Store
from scheduler.tools import Tools

NOW = datetime(2026, 10, 19, 9, tzinfo=ZoneInfo("Asia/Shanghai"))


def ev(title, start="10:00", end="11:00", day="2026-10-19"):
    return dict(title=title, start=f"{day}T{start}:00+08:00", end=f"{day}T{end}:00+08:00")


@pytest.fixture
def ctx(tmp_path):
    s = Store(tmp_path / "synthetic.sqlite3", "Asia/Shanghai")
    return s, s.new_session()


def seed(ctx):
    s, _ = ctx
    return [
        s.calendar.create(ev("面试")),
        s.calendar.create(ev("羽毛球", "15:00", "16:00")),
        s.calendar.create(ev("晚餐", "18:00", "19:00")),
    ]


def cancel(ctx, **kw):
    from scheduler.trash import propose_cancel

    return propose_cancel(*ctx, {"kind": "today"} | kw, now=NOW)


def restore(ctx, events):
    from scheduler.trash import propose_restore

    return propose_restore(*ctx, event_ids=[e["id"] for e in events])


def test_A_scope_and_no_write_until_confirmation(ctx):
    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    assert p["action"] == "cancel_batch" and len(p["event"]["selected"]) == 3
    assert s.calendar.search() == events
    r = s.confirm(sid, p["id"], True)
    assert r["status"] == "committed" and r["count"] == 3
    assert s.calendar.search() == []
    assert len(s.calendar.trash()) == 3
    assert all(s.calendar.get(e["id"]) is None for e in events)
    assert s.confirm(sid, p["id"], True) == r
    assert s.calendar.create(ev("新面试"))


def test_A_empty_afternoon_week_boundaries(ctx):
    s, sid = ctx
    assert cancel(ctx) is None
    seed(ctx)
    s.calendar.create(ev("次日午夜", "00:00", "01:00", day="2026-10-20"))
    p = cancel(ctx, kind="afternoon")
    assert [x["title"] for x in p["event"]["selected"]] == ["羽毛球"]
    p = cancel(ctx)
    assert len(p["event"]["selected"]) == 3
    p = cancel(ctx, kind="week")
    assert p["event"]["range"] == {
        "start": "2026-10-19T00:00:00+08:00",
        "end": "2026-10-26T00:00:00+08:00",
    }
    assert len(p["event"]["selected"]) == 4


def test_B_exclude_and_ambiguous(ctx):
    from scheduler.trash import revise_cancel

    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    p = revise_cancel(s, sid, p["id"], [events[0]["id"], events[2]["id"]])
    assert len(p["event"]["selected"]) == 2 and p["event"]["excluded_ids"] == [events[1]["id"]]
    s.confirm(sid, p["id"], True)
    assert s.calendar.search() == [events[1]]


def test_C_new_or_changed_event_invalidates_all(ctx):
    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    s.calendar.update(events[0]["id"], 1, {"notes": "new"})
    assert s.confirm(sid, p["id"], True)["error"] == "stale_batch"
    assert len(s.calendar.search()) == 3 and not s.calendar.trash()
    p = cancel(ctx)
    s.calendar.create(ev("新增", "20:00", "21:00"))
    assert s.confirm(sid, p["id"], True)["error"] == "stale_batch"
    assert len(s.calendar.search()) == 4


def test_C_cancel_noop_and_expired_proposal(ctx):
    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    assert s.confirm(sid, p["id"], False)["status"] == "cancelled"
    assert s.calendar.search() == events
    p = cancel(ctx)
    s.invalidate(sid)
    with pytest.raises(CalendarError, match="proposal_expired"):
        s.confirm(sid, p["id"], True)


def test_D_restore_original_ids_and_versions(ctx):
    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    receipt = s.confirm(sid, p["id"], True)
    restored = restore(ctx, [events[0]])
    assert s.confirm(sid, restored["id"], True)["status"] == "committed"
    assert s.calendar.get(events[0]["id"])["version"] == 3
    from scheduler.trash import propose_restore

    remaining = propose_restore(s, sid, operation_id=receipt["operation_id"])
    result = s.confirm(sid, remaining["id"], True)
    assert result["status"] == "committed" and result["count"] == 2
    assert not s.calendar.trash()
    assert len(Store(s.calendar.path, "Asia/Shanghai").calendar.search()) == 3


def test_D_restore_conflicts_explicit_subset(ctx):
    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    s.confirm(sid, p["id"], True)
    active = s.calendar.create(ev("占用"))
    from scheduler.trash import propose_restore

    result = propose_restore(s, sid, event_ids=[e["id"] for e in events])
    assert result["action"] == "restore_batch"
    assert result["event"]["conflicts"][0]["overlap"]["start"].endswith("10:00:00+08:00")
    assert len(result["event"]["feasible_ids"]) == 2
    blocked = s.confirm(sid, result["id"], True)
    assert blocked["error"] == "restore_conflict" and s.calendar.search() == [active]
    # Separate, explicit proposal for exactly the feasible subset; no silent partial restore.
    subset = propose_restore(s, sid, event_ids=result["event"]["feasible_ids"])
    assert s.confirm(sid, subset["id"], True)["count"] == 2
    assert s.calendar.trash()[0]["id"] == events[0]["id"]


def test_D_expired_cleanup_bounded_never_active(ctx):
    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    s.confirm(sid, p["id"], True)
    active = s.calendar.create(ev("活动", "20:00", "21:00"))
    with s.calendar.connect() as db:
        db.execute(
            "UPDATE events SET deleted_at=? WHERE id=?",
            (int(time.time()) - 8 * 86400, events[0]["id"]),
        )
    with pytest.raises(CalendarError, match="trash_expired"):
        restore(ctx, [events[0]])
    assert s.calendar.cleanup_trash(limit=1) == 1
    assert s.calendar.cleanup_trash(limit=1) == 0
    assert s.calendar.get(active["id"]) == active and len(s.calendar.trash()) == 2


@pytest.mark.parametrize("mode", ["delete", "restore"])
def test_E_second_update_failure_rolls_all_back(ctx, mode):
    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    if mode == "restore":
        s.confirm(sid, p["id"], True)
        p = restore(ctx, events)
    with s.calendar.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_second BEFORE UPDATE ON events WHEN NEW.title='羽毛球' BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    result = s.confirm(sid, p["id"], True)
    assert result["status"] == "error"
    assert len(s.calendar.search()) == (3 if mode == "delete" else 0)
    assert len(s.calendar.trash()) == (0 if mode == "delete" else 3)
    assert s.confirm(sid, p["id"], True) == result


def test_E_post_write_verification_rollback(ctx, monkeypatch):
    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    monkeypatch.setattr(s.calendar, "trash_get", lambda *args, **kw: None)
    assert s.confirm(sid, p["id"], True)["error"] == "verification_failed"
    assert s.calendar.search() == events


def test_B_tool_exclusions_continuation_and_ambiguity(ctx):
    s, sid = ctx
    events = seed(ctx)
    from scheduler.agent import Agent
    from scheduler.config import Settings
    from tests.helpers import ScriptedModel, call
    import asyncio

    model = ScriptedModel(
        call("propose_cancel_range", scope={"kind": "today"}),
        call("propose_cancel_range", continue_previous=True, exclude_queries=["羽毛球"]),
    )
    agent = Agent(s, model, Settings(), now=lambda: NOW)
    first = asyncio.run(agent.run(sid, "取消今天的所有安排。"))
    assert first["proposal"]["action"] == "cancel_batch"
    second = asyncio.run(agent.run(sid, "保留下午的羽毛球，其他都取消。"))
    assert len(second["proposal"]["event"]["selected"]) == 2
    assert second["proposal"]["event"]["excluded_ids"] == [events[1]["id"]]
    with pytest.raises(CalendarError, match="proposal_expired"):
        s.confirm(sid, first["proposal"]["id"], True)
    s.calendar.create(ev("羽毛球", "20:00", "21:00"))
    tool = Tools(s, sid, now=NOW)
    ambiguous = tool.execute(
        "propose_cancel_range", {"scope": {"kind": "today"}, "exclude_queries": ["羽毛球"]}
    )
    assert "question" in ambiguous
    assert s.confirm(sid, s.pending(sid)["id"], True)["error"] == "invalid_selection"
    assert len(s.calendar.search()) == 4


def test_C_unrelated_turn_cannot_reuse_selection(ctx):
    import asyncio
    from scheduler.agent import Agent
    from scheduler.config import Settings
    from tests.helpers import ScriptedModel, call

    s, sid = ctx
    seed(ctx)
    p = cancel(ctx)
    result = asyncio.run(
        Agent(s, ScriptedModel(call("ask_user", fields=["date"])), Settings()).run(
            sid, "另一个任务：帮我安排旅行"
        )
    )
    assert result["status"] == "clarification"
    with pytest.raises(CalendarError, match="proposal_expired"):
        s.confirm(sid, p["id"], True)
    # A NEW range operation must start with all events, not an earlier selection.
    from scheduler.trash import revise_cancel

    p = cancel(ctx)
    revised = revise_cancel(s, sid, p["id"], [p["event"]["selected"][0]["id"]])
    assert len(cancel(ctx)["event"]["selected"]) == 3
    with pytest.raises(CalendarError):
        s.confirm(sid, revised["id"], True)


def test_D_undo_scoped_to_operation_and_old_batch_cannot_restore_new_tombstone(ctx):
    from scheduler.trash import propose_restore

    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    r = s.confirm(sid, p["id"], True)
    original = r["operation_id"]
    one = restore(ctx, [events[0]])
    s.confirm(sid, one["id"], True)
    again = cancel(ctx)
    s.confirm(sid, again["id"], True)
    undo = propose_restore(s, sid, operation_id=original)
    assert {e["id"] for e in undo["event"]["selected"]} == {events[1]["id"], events[2]["id"]}
    s.confirm(sid, undo["id"], True)
    assert s.calendar.trash_get(events[0]["id"])["deletion_batch_id"] == again["id"]
    other = s.new_session()
    with pytest.raises(CalendarError, match="operation_not_found"):
        propose_restore(s, other, operation_id=original)


def test_D_restore_conflict_appearing_after_review_rolls_back(ctx):
    s, sid = ctx
    events = seed(ctx)
    s.confirm(sid, cancel(ctx)["id"], True)
    p = restore(ctx, events)
    active = s.calendar.create(ev("新占用"))
    assert s.confirm(sid, p["id"], True)["error"] == "restore_conflict"
    assert s.calendar.search() == [active] and len(s.calendar.trash()) == 3


def test_D_feasible_subset_skips_blocked_predecessor(ctx):
    s, sid = ctx
    a = s.calendar.create(ev("长活动", "09:00", "12:00"))
    s.confirm(sid, cancel(ctx)["id"], True)
    b = s.calendar.create(ev("后半段", "11:00", "13:00"))
    s.confirm(sid, cancel(ctx)["id"], True)
    s.calendar.create(ev("只挡前半段", "09:00", "10:00"))
    p = restore(ctx, [a, b])
    assert p["event"]["feasible_ids"] == [b["id"]]


def test_A_more_than_search_limit_and_cross_midnight(ctx):
    from datetime import timedelta

    s, sid = ctx
    for i in range(60):
        start = NOW.replace(hour=10) + timedelta(minutes=5 * i)
        s.calendar.create(
            {
                "title": str(i),
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=5)).isoformat(),
            }
        )
    assert len(cancel(ctx)["event"]["selected"]) == 60
    overnight = s.calendar.create(
        {"title": "跨日", "start": "2026-10-18T23:00:00+08:00", "end": "2026-10-19T00:30:00+08:00"}
    )
    assert overnight["id"] in {e["id"] for e in cancel(ctx)["event"]["selected"]}


def test_migration_legacy_soft_delete_and_active_final_batch(ctx):
    s, sid = ctx
    a = s.calendar.create(ev("A"))
    p = s.propose(sid, "delete", ev("A"), a)
    receipt = s.confirm(sid, p["id"], True)
    assert receipt["operation_id"] == p["id"]
    assert s.calendar.trash_get(a["id"])["id"] == a["id"]
    # Deleted rows must be excluded from final-state scheduling validation too.
    changes = [{"action": "create", "event": ev("B")}]
    result = s.propose(sid, "batch", {"changes": changes})
    assert s.confirm(sid, result["id"], True)["status"] == "committed"


def test_http_checkbox_cross_session_trash_undo_and_restore_conflicts(ctx):
    from fastapi.testclient import TestClient
    from scheduler.app import create_app
    from scheduler.config import Settings
    from tests.helpers import ScriptedModel

    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    app = create_app(Settings(database=str(s.calendar.path)), ScriptedModel())
    client = TestClient(app)
    client.cookies.set("schedule_session", sid)
    assert (
        TestClient(app)
        .post(
            "/api/cancellations/" + p["id"] + "/selection", json={"selected_ids": [events[0]["id"]]}
        )
        .status_code
        == 409
    )
    changed = client.post(
        "/api/cancellations/" + p["id"] + "/selection",
        json={"selected_ids": [events[0]["id"], events[2]["id"]]},
    ).json()["proposal"]
    assert len(changed["event"]["selected"]) == 2
    receipt = client.post("/api/proposals/" + changed["id"], json={"approve": True}).json()
    assert receipt["status"] == "committed"
    trash = client.get("/api/trash").json()["events"]
    assert {e["id"] for e in trash} == {events[0]["id"], events[2]["id"]}
    assert client.get("/api/state").json()["undo"]["operation_id"] == changed["id"]
    undo = client.post("/api/undo/" + changed["id"], json={}).json()
    assert undo["action"] == "restore_batch" and undo["status"] == "committed"
    assert undo["count"] == 2
    assert client.get("/api/state").json()["undo"] is None
    assert len(s.calendar.search()) == 3


@pytest.mark.parametrize("mutation", ["title='corrupted'", "deleted_at=deleted_at-8*86400"])
def test_E_later_delete_trigger_cannot_corrupt_previous_tombstone(ctx, mutation):
    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    with s.calendar.connect() as db:
        db.execute(
            f"CREATE TRIGGER corrupt_previous AFTER UPDATE ON events WHEN NEW.title='羽毛球' AND NEW.deleted_at IS NOT NULL BEGIN UPDATE events SET {mutation} WHERE title='面试'; END"
        )
    r = s.confirm(sid, p["id"], True)
    assert r["error"] == "verification_failed"
    assert s.calendar.search() == events and not s.calendar.trash()


def test_F_ui_restore_conflict_stays_disabled_after_loading():
    import subprocess

    result = subprocess.run(["node", "tests/ui_trash_check.cjs"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_B_unresolved_exclusion_survives_continuation(ctx):
    from scheduler.trash import propose_cancel

    s, sid = ctx
    a = s.calendar.create(ev("王会A"))
    s.calendar.create(ev("王会B", "12:00", "13:00"))
    first = propose_cancel(s, sid, {"kind": "today"}, exclude_queries=["王会"], now=NOW)
    assert first.get("question")
    continued = propose_cancel(s, sid, continue_previous=True)
    assert continued.get("question")
    assert s.confirm(sid, continued["review"]["id"], True)["error"] == "invalid_selection"
    assert len(s.calendar.search()) == 2
    # Restart fresh review; one explicit observed ID resolves that ambiguous group.
    propose_cancel(s, sid, {"kind": "today"}, exclude_queries=["王会"], now=NOW)
    resolved = propose_cancel(s, sid, continue_previous=True, exclude_ids=[a["id"]])
    assert not resolved["event"]["unresolved_exclusion"]
    assert s.confirm(sid, resolved["id"], True)["count"] == 1
    assert s.calendar.get(a["id"]) == a


def test_A_week_end_and_dst_scope(ctx):
    from scheduler.trash import resolve_range
    from scheduler.calendar import Calendar

    s, sid = ctx
    s.calendar.create(ev("周日", "23:00", "23:30", day="2026-10-25"))
    s.calendar.create(ev("下周一", "00:00", "01:00", day="2026-10-26"))
    assert [e["title"] for e in cancel(ctx, kind="week")["event"]["selected"]] == ["周日"]
    ny = Calendar(s.calendar.path.parent / "dst.sqlite3", "America/New_York")
    bounds = resolve_range(ny, {"kind": "date", "date": "2026-11-01"})
    assert ny.timestamp(bounds["end"]) - ny.timestamp(bounds["start"]) == 25 * 3600


def test_B_large_selection_fits_validated_endpoint(ctx):
    from fastapi.testclient import TestClient
    from scheduler.app import create_app
    from scheduler.config import Settings
    from tests.helpers import ScriptedModel
    from uuid import uuid4

    s, sid = ctx
    base = s.calendar.timestamp("2026-10-19T00:00:00+08:00")
    rows = [
        (uuid4().hex, str(i), base + i * 60, base + (i + 1) * 60, "", "", 1) for i in range(1000)
    ]
    with s.calendar.connect() as db:
        db.executemany(
            "INSERT INTO events(id,title,start,end,location,notes,version) VALUES(?,?,?,?,?,?,?)",
            rows,
        )
    p = cancel(ctx)
    client = TestClient(create_app(Settings(database=str(s.calendar.path)), ScriptedModel()))
    client.cookies.set("schedule_session", sid)
    response = client.post(
        "/api/cancellations/" + p["id"] + "/selection",
        json={"selected_ids": [r[0] for r in rows[1:]]},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["proposal"]["event"]["selected"]) == 999


def test_E_concurrent_confirmation_and_stale_restore_snapshot(ctx):
    from concurrent.futures import ThreadPoolExecutor
    from scheduler.trash import propose_restore

    s, sid = ctx
    events = seed(ctx)
    p = cancel(ctx)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: s.confirm(sid, p["id"], True), range(2)))
    assert receipts[0] == receipts[1]
    assert all(s.calendar.trash_get(e["id"])["version"] == 2 for e in events)
    snapshot = s.calendar.trash()
    restore_proposal = restore(ctx, [events[0]])
    s.confirm(sid, restore_proposal["id"], True)
    recancel = cancel(ctx)
    s.confirm(sid, recancel["id"], True)
    with pytest.raises(CalendarError, match="stale_trash"):
        propose_restore(
            s,
            sid,
            event_ids=[events[0]["id"]],
            expected=[e for e in snapshot if e["id"] == events[0]["id"]],
        )


def test_E_restore_post_write_verification_failure(ctx, monkeypatch):
    s, sid = ctx
    events = seed(ctx)
    s.confirm(sid, cancel(ctx)["id"], True)
    p = restore(ctx, events)
    monkeypatch.setattr(s.calendar, "get", lambda *args, **kw: None)
    r = s.confirm(sid, p["id"], True)
    assert r["error"] == "verification_failed"
    monkeypatch.undo()
    assert not s.calendar.search() and len(s.calendar.trash()) == 3


def test_F_real_scope_string_error_is_specific_and_strict(ctx):
    from scheduler.tools import SCHEMAS

    s, sid = ctx
    seed(ctx)
    result = Tools(s, sid, now=NOW).execute("propose_cancel_range", {"scope": '{"kind":"today"}'})
    assert result["error"] == "invalid_arguments"
    assert {"path": "scope", "type": "model_type"} in result["issues"]
    assert "JSON object" in result["hint"] and "字符串" in result["hint"]
    schema = next(
        x["function"]["parameters"]
        for x in SCHEMAS
        if x["function"]["name"] == "propose_cancel_range"
    )
    assert schema["properties"]["scope"]["type"] == "object"
    assert s.pending(sid) is None and len(s.calendar.search()) == 3


def test_F_trash_observation_allows_followup_restore_in_same_turn(ctx):
    import asyncio
    from scheduler.agent import Agent
    from scheduler.config import Settings
    from tests.helpers import ScriptedModel, call

    s, sid = ctx
    events = seed(ctx)
    s.confirm(sid, cancel(ctx)["id"], True)
    model = ScriptedModel(
        call("search_trash", query="羽毛球"), call("propose_restore", event_ids=[events[1]["id"]])
    )
    result = asyncio.run(Agent(s, model, Settings()).run(sid, "恢复删除的羽毛球"))
    assert result["status"] == "proposal" and result["proposal"]["action"] == "restore_batch"
    assert len(model.messages) == 2
    assert s.confirm(sid, result["proposal"]["id"], True)["count"] == 1


def test_F_trash_query_can_finish_after_observation(ctx):
    import asyncio
    from scheduler.agent import Agent
    from scheduler.config import Settings
    from tests.helpers import ScriptedModel, call

    s, sid = ctx
    model = ScriptedModel(call("search_trash"), {"role": "assistant", "content": "回收站为空"})
    result = asyncio.run(Agent(s, model, Settings()).run(sid, "查看回收站"))
    assert result["status"] == "query" and result["view"] == "trash"
    assert len(model.messages) == 2


def test_D_ui_undo_is_immediate_explicit_and_idempotent(ctx):
    from fastapi.testclient import TestClient
    from scheduler.app import create_app
    from scheduler.config import Settings
    from tests.helpers import ScriptedModel

    s, sid = ctx
    seed(ctx)
    operation = s.confirm(sid, cancel(ctx)["id"], True)
    client = TestClient(create_app(Settings(database=str(s.calendar.path)), ScriptedModel()))
    client.cookies.set("schedule_session", sid)
    first = client.post("/api/undo/" + operation["operation_id"], json={}).json()
    assert first["status"] == "committed" and first["count"] == 3
    assert len(s.calendar.search()) == 3
    assert client.post("/api/undo/" + operation["operation_id"], json={}).json() == first
    assert all(e["version"] == 3 for e in s.calendar.search())


def test_D_immediate_undo_with_conflict_returns_review_without_partial_restore(ctx):
    from fastapi.testclient import TestClient
    from scheduler.app import create_app
    from scheduler.config import Settings
    from tests.helpers import ScriptedModel

    s, sid = ctx
    events = seed(ctx)
    operation = s.confirm(sid, cancel(ctx)["id"], True)
    active = s.calendar.create(ev("占用"))
    client = TestClient(create_app(Settings(database=str(s.calendar.path)), ScriptedModel()))
    client.cookies.set("schedule_session", sid)
    result = client.post("/api/undo/" + operation["operation_id"], json={}).json()
    assert result["status"] == "proposal" and result["proposal"]["event"]["conflicts"]
    assert len(result["proposal"]["event"]["feasible_ids"]) == 2
    assert s.calendar.search() == [active] and len(s.calendar.trash()) == 3
    selected = [
        e
        for e in result["proposal"]["event"]["selected"]
        if e["id"] in result["proposal"]["event"]["feasible_ids"]
    ]
    references = [{k: e[k] for k in ("id", "version", "deletion_batch_id")} for e in selected]
    subset = client.post("/api/trash/restore", json={"selected": references}).json()["proposal"]
    assert (
        client.post("/api/proposals/" + subset["id"], json={"approve": True}).json()["count"] == 2
    )
    assert s.calendar.trash()[0]["id"] == events[0]["id"]

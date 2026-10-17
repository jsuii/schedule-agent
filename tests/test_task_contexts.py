"""Synthetic task lifecycle, routing and slot safety; NO real LLM."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scheduler.agent import Agent
from scheduler.config import Settings
from scheduler.storage import Store
from scheduler.tools import Tools
from tests.helpers import ScriptedModel, call

NOW = datetime(2026, 10, 17, 11, tzinfo=ZoneInfo("Asia/Shanghai"))


def at(day, time):
    return f"2026-10-{day}T{time}:00+08:00"


@pytest.fixture
def ctx(tmp_path):
    s = Store(tmp_path / "contexts.sqlite3", "Asia/Shanghai")
    return s, s.new_session()


def control(ctx, action="new", **kw):
    return Tools(*ctx).execute("task_context", dict(action=action, **kw))


def start(ctx, title="邮件", entity="event"):
    r = control(ctx, title=title, entity=entity)
    assert not r.get("error"), r
    return ctx[0].session(ctx[1])["state"]["context_id"]


def draft_email(ctx):
    return Tools(*ctx).execute(
        "stage_task",
        dict(
            intents=[
                dict(
                    intent_id="email",
                    event=dict(
                        title="给导师发邮件", start=at("17", "15:00"), notes="确认推荐信的事情"
                    ),
                )
            ]
        ),
    )


def flexible(ctx, **kw):
    return Tools(*ctx).execute(
        "schedule_flexible", dict(title="理发", date="2026-10-20", duration_minutes=60, **kw)
    )


def test_recorded_root_cause_old_gate_instead_of_failed_slot_search(ctx):
    draft_email(ctx)
    slots = Tools(*ctx).execute(
        "find_slots", dict(start=at("20", "12:00"), end=at("20", "18:00"), duration_minutes=60)
    )
    assert slots["slots"]
    result = Tools(*ctx).execute(
        "propose_create",
        dict(title="理发", start=slots["slots"][0]["start"], end=slots["slots"][0]["end"]),
    )
    assert result["error"] == "pending_task"
    assert ctx[0].session(ctx[1])["state"]["task"]["intents"][0]["event"]["title"] == "给导师发邮件"


def test_new_request_suspends_old_fields_and_explicit_resume_cancel(ctx):
    s, sid = ctx
    email = start(ctx)
    draft_email(ctx)
    original = s.session(sid)["state"]["task"]
    hair = start(ctx, "理发")
    assert hair != email and not s.session(sid)["state"].get("task")
    result = flexible(ctx)
    assert result.get("slot_review") and "邮件" not in result["question"]
    review = s.session(sid)["state"]["slot_review"]
    assert control(ctx, "resume", task_id=email).get("context")
    assert (
        s.session(sid)["state"]["task"] == original and "slot_review" not in s.session(sid)["state"]
    )
    assert control(ctx, "resume", task_id=hair).get("context")
    assert s.session(sid)["state"]["slot_review"] == review
    assert control(ctx, "cancel", task_id=email).get("question")
    assert s.session(sid)["state"]["context_id"] == hair
    assert flexible(ctx)["slot_review"]
    assert control(ctx, "resume", task_id=email)["error"] == "task_unavailable"


def test_flexible_selection_revalidates_and_requires_confirmation(ctx):
    from scheduler.flow import choose_slot

    s, sid = ctx
    start(ctx, "理发")
    s.calendar.create(dict(title="已有安排", start=at("20", "12:00"), end=at("20", "15:00")))
    r = flexible(ctx)
    review = r["slot_review"]
    assert 2 <= len(review["candidates"]) <= 4
    assert all(
        e["start"] >= at("20", "15:00") and e["end"] <= at("20", "18:00")
        for e in review["candidates"]
    )
    before = s.calendar.search()
    selected = review["candidates"][0]
    p = choose_slot(s, sid, review["id"], selected["id"])
    assert s.calendar.search() == before and p["action"] == "create"
    receipt = s.confirm(sid, p["id"], True)
    assert receipt["status"] == "committed"
    assert s.confirm(sid, p["id"], True) == receipt
    assert len(s.calendar.search()) == 2


def test_slot_stale_cross_session_empty_and_direct_answer(ctx):
    from scheduler.flow import choose_slot
    from scheduler.calendar import CalendarError

    s, sid = ctx
    start(ctx, "理发")
    r = flexible(ctx, not_before=at("20", "15:00"))["slot_review"]
    assert all(x["start"] >= at("20", "15:00") for x in r["candidates"])
    opt = r["candidates"][0]
    with pytest.raises(CalendarError):
        choose_slot(s, s.new_session(), r["id"], opt["id"])
    s.calendar.create(dict(title="新占用", start=opt["start"], end=opt["end"]))
    with pytest.raises(CalendarError):
        choose_slot(s, sid, r["id"], opt["id"])
    assert s.pending(sid) is None
    # A direct clarification update retains task identity and searches new bounds.
    identity = s.session(sid)["state"]["context_id"]
    control(ctx, "continue", task_id=identity)
    assert flexible(ctx, not_before=at("20", "16:00"))["slot_review"]["candidates"][0][
        "start"
    ] >= at("20", "16:00")
    s.calendar.create(dict(title="占满另一天", start=at("21", "12:00"), end=at("21", "18:00")))
    none = Tools(*ctx).execute(
        "schedule_flexible", dict(title="理发", date="2026-10-21", duration_minutes=60)
    )
    assert not none["slot_review"]["candidates"] and "没有" in none["question"]


def test_reminder_capability_preserved_without_inventing_duration(ctx):
    s, sid = ctx
    r = control(
        ctx,
        title="给导师发邮件",
        entity="reminder",
        reminder_at=at("17", "15:00"),
        notes="确认推荐信的事情",
    )
    assert "不支持定时提醒" in r["question"] and "持续时间" not in r["question"]
    old = s.session(sid)["state"]["context_id"]
    assert s.session(sid)["state"]["reminder"]["notes"] == "确认推荐信的事情"
    start(ctx, "理发")
    assert flexible(ctx).get("slot_review")
    control(ctx, "resume", task_id=old)
    assert s.session(sid)["state"]["reminder"]["at"] == at("17", "15:00")
    assert s.calendar.search() == []


def test_pending_proposal_is_suspended_and_cannot_execute_during_other_task(ctx):
    from scheduler.calendar import CalendarError

    s, sid = ctx
    old = start(ctx, "会议")
    p = Tools(*ctx).execute(
        "propose_create", dict(title="会议", start=at("20", "09:00"), end=at("20", "10:00"))
    )["proposal"]
    start(ctx, "理发")
    with pytest.raises(CalendarError):
        s.confirm(sid, p["id"], True)
    assert s.calendar.search() == []
    control(ctx, "resume", task_id=old)
    assert s.pending(sid)["id"] == p["id"]
    assert s.confirm(sid, p["id"], True)["status"] == "committed"


def test_contexts_survive_confirmation_of_another_task(ctx):
    from scheduler.flow import choose_slot, contexts

    s, sid = ctx
    email = start(ctx)
    draft_email(ctx)
    hair = start(ctx, "理发")
    r = flexible(ctx)["slot_review"]
    p = choose_slot(s, sid, r["id"], r["candidates"][0]["id"])
    assert s.confirm(sid, p["id"], True)["status"] == "committed"
    assert any(x["id"] == email and x["status"] == "suspended" for x in contexts(s, sid))
    control(ctx, "resume", task_id=email)
    assert s.session(sid)["state"]["task"]["intents"][0]["event"]["notes"] == "确认推荐信的事情"
    assert any(x["id"] == hair and x["status"] == "completed" for x in contexts(s, sid))


def test_agent_requires_explicit_context_decision_before_merging_old_task(ctx):
    s, sid = ctx
    draft_email(ctx)
    r = asyncio.run(
        Agent(
            s,
            ScriptedModel(
                call("stage_task", intents=[dict(intent_id="hair", event=dict(title="理发"))]),
                call("task_context", action="new", title="理发"),
                call("schedule_flexible", title="理发", date="2026-10-20", duration_minutes=60),
            ),
            Settings(),
            now=lambda: NOW,
        ).run(sid, "10/20下午去理发，预留一个小时，具体几点你帮我找个合适的时间。")
    )
    assert r["trace"][0]["result"]["error"] == "context_required"
    assert r["status"] == "clarification" and "邮件" not in r["message"]
    assert s.session(sid)["state"]["slot_review"]["title"] == "理发"
    assert not s.session(sid)["state"].get("task")


def test_ambiguous_context_asks_one_disambiguation_without_changing_task(ctx):
    s, sid = ctx
    old = start(ctx)
    draft_email(ctx)
    result = control(ctx, "clarify")
    assert "新安排" in result["question"] and s.session(sid)["state"]["context_id"] == old
    assert s.pending(sid) is None


def test_exact_user_sequence_shared_sequential_reminder_then_flexible(ctx):
    s, sid = ctx

    def continue_current(_):
        return call(
            "task_context", action="continue", task_id=s.session(sid)["state"]["context_id"]
        )

    model = ScriptedModel(
        call(
            "stage_task",
            intents=[
                dict(intent_id="grocery", event=dict(title="买菜")),
                dict(intent_id="breakfast", event=dict(title="吃早餐")),
            ],
            time_windows=[
                dict(
                    window_id="w",
                    intent_ids=["grocery", "breakfast"],
                    start=at("18", "09:00"),
                    end=at("18", "10:00"),
                )
            ],
        ),
        continue_current,
        call(
            "stage_task",
            intents=[
                dict(
                    intent_id="breakfast", event=dict(start=at("18", "09:00")), duration_minutes=15
                )
            ],
            relations=[
                dict(first="breakfast", second="grocery", kind="sequential", confirmed=True)
            ],
        ),
        continue_current,
        call(
            "stage_task",
            intents=[
                dict(intent_id="grocery", event=dict(start=at("18", "09:15")), duration_minutes=45)
            ],
        ),
        call("task_context", action="new", title="羽毛球与甜品"),
        call(
            "stage_task",
            intents=[
                dict(
                    intent_id="badminton",
                    event=dict(title="羽毛球", start=at("20", "14:00")),
                    duration_minutes=90,
                ),
                dict(
                    intent_id="dessert",
                    event=dict(title="甜品", start=at("20", "15:30")),
                    duration_minutes=90,
                ),
            ],
            relations=[
                dict(first="badminton", second="dessert", kind="sequential", confirmed=True)
            ],
        ),
        call(
            "task_context",
            action="new",
            title="给导师发邮件",
            entity="reminder",
            reminder_at=at("17", "15:00"),
            notes="确认推荐信的事情",
        ),
        call("task_context", action="new", title="理发"),
        call("schedule_flexible", title="理发", date="2026-10-20", duration_minutes=60),
    )
    agent = Agent(s, model, Settings(), now=lambda: NOW)

    def run(text):
        return asyncio.run(agent.run(sid, text))

    assert run("明天早晨9-10点去买菜、吃早餐")["status"] == "clarification"
    partial = run("吃早餐再买菜，吃早餐15分钟")
    assert partial["status"] == "clarification" and "买菜各需要多久" in partial["message"]
    p = run("买菜45分钟")["proposal"]
    assert s.calendar.search() == []
    assert s.confirm(sid, p["id"], True)["status"] == "committed"
    assert [(e["title"], e["start"][11:16], e["end"][11:16]) for e in s.calendar.search()] == [
        ("吃早餐", "09:00", "09:15"),
        ("买菜", "09:15", "10:00"),
    ]
    p = run("10/20下午2点打一个半小时羽毛球，结束后和球友吃甜品，吃一个半小时。")["proposal"]
    assert [
        (e["after"]["start"][11:16], e["after"]["end"][11:16]) for e in p["event"]["preview"]
    ] == [("14:00", "15:30"), ("15:30", "17:00")]
    assert s.confirm(sid, p["id"], True)["status"] == "committed"
    reminder = run("今天下午三点提醒我给导师发邮件，内容是确认推荐信的事情。")
    assert "不支持定时提醒" in reminder["message"] and "持续时间" not in reminder["message"]
    old = reminder["context_id"]
    hair = run("10/20下午去理发，预留一个小时，具体几点你帮我找个合适的时间。")
    assert hair["status"] == "clarification" and "邮件" not in hair["message"]
    from scheduler.flow import contexts

    assert any(c["id"] == old and c["status"] == "suspended" for c in contexts(s, sid))
    assert s.session(sid)["state"]["slot_review"]["candidates"] and len(s.calendar.search()) == 4


def test_cancel_suspended_task_keeps_other_pending_proposal(ctx):
    s, sid = ctx
    email = start(ctx)
    draft_email(ctx)
    hair = start(ctx, "理发")
    p = Tools(*ctx).execute(
        "propose_create", dict(title="理发", start=at("20", "12:00"), end=at("20", "13:00"))
    )["proposal"]
    reply = asyncio.run(
        Agent(
            s, ScriptedModel(call("task_context", action="cancel", task_id=email)), Settings()
        ).run(sid, "先不管给导师发邮件的事")
    )
    assert "已取消" in reply["message"] and s.session(sid)["state"]["context_id"] == hair
    assert s.pending(sid)["id"] == p["id"] and s.calendar.search() == []


def test_failure_then_independent_request_and_scope_ids(ctx):
    s, sid = ctx
    model = ScriptedModel(
        call("task_context", action="new", title="旧任务"),
        call("stage_task", intents=[dict(intent_id="a", event=dict(title="旧任务", start="bad"))]),
        call("task_context", action="new", title="理发"),
        call("schedule_flexible", title="理发", date="2026-10-20", duration_minutes=60),
    )
    agent = Agent(s, model, Settings(max_iterations=2))
    old = asyncio.run(agent.run(sid, "一个错误的旧任务"))
    assert old["status"] == "error"
    newer = asyncio.run(agent.run(sid, "10/20下午去理发，预留一小时"))
    assert newer["status"] == "clarification" and newer["context_id"] != old["context_id"]
    assert not s.session(sid)["state"].get("task")


def test_failed_correction_cannot_revive_old_confirmation(ctx):
    from scheduler.provider import ModelError
    from scheduler.calendar import CalendarError

    s, sid = ctx
    identity = start(ctx, "会议")
    p = Tools(*ctx).execute(
        "propose_create", dict(title="会议", start=at("20", "09:00"), end=at("20", "10:00"))
    )["proposal"]
    model = ScriptedModel(
        call("task_context", action="continue", task_id=identity), ModelError("模型超时")
    )
    reply = asyncio.run(Agent(s, model, Settings()).run(sid, "改到十点开始，一小时"))
    assert reply["status"] == "error"
    assert s.pending(sid) is None
    with pytest.raises(CalendarError):
        s.confirm(sid, p["id"], True)
    assert s.calendar.search() == []


def test_http_context_ownership_slots_and_request_binding(ctx):
    from fastapi.testclient import TestClient
    from scheduler.app import create_app

    s, sid = ctx
    old = start(ctx)
    draft_email(ctx)
    hair = start(ctx, "理发")
    review = flexible(ctx)["slot_review"]
    app = create_app(
        Settings(database=str(s.calendar.path)), ScriptedModel(call("ask_user", fields=["details"]))
    )
    client = TestClient(app)
    client.cookies.set("schedule_session", sid)
    state = client.get("/api/state").json()
    assert state["active_context_id"] == hair and state["slot_review"]["id"] == review["id"]
    assert any(c["id"] == old and c["status"] == "suspended" for c in state["contexts"])
    outsider = TestClient(app)
    assert outsider.post("/api/contexts/" + old + "/resume", json={}).status_code == 404
    url = "/api/slots/" + review["id"] + "/" + review["candidates"][0]["id"]
    assert outsider.post(url, json={}).status_code == 409
    proposal = client.post(url, json={}).json()["proposal"]
    assert client.get("/api/state").json()["slot_review"] is None
    assert s.calendar.search() == []
    assert (
        client.post("/api/proposals/" + proposal["id"], json={"approve": True}).json()["status"]
        == "committed"
    )
    assert client.post("/api/contexts/" + old + "/resume", json={}).status_code == 200
    assert client.get("/api/state").json()["active_context_id"] == old
    assert client.post("/api/contexts/" + old + "/cancel", json={}).status_code == 200
    response = client.post(
        "/api/chat", json={"text": "一个新问题", "request_id": "bound-id"}
    ).json()
    assert response["request_id"] == "bound-id"
    assert (
        client.post("/api/chat", json={"text": "一个新问题", "request_id": "bound-id"}).json()
        == response
    )


def test_expired_and_superseded_slots_cannot_prepare_proposal(ctx):
    from scheduler.flow import choose_slot
    from scheduler.calendar import CalendarError

    s, sid = ctx
    start(ctx, "理发")
    first = flexible(ctx)["slot_review"]
    current = flexible(ctx, not_before=at("20", "15:00"))["slot_review"]
    with pytest.raises(CalendarError):
        choose_slot(s, sid, first["id"], first["candidates"][0]["id"])
    state = s.session(sid)["state"]
    state["slot_review"]["expires"] = 0
    s.save_state(sid, state)
    with pytest.raises(CalendarError):
        choose_slot(s, sid, current["id"], current["candidates"][0]["id"])
    assert s.calendar.search() == [] and s.pending(sid) is None


def test_slot_write_failure_rolls_back_and_context_is_failed(ctx):
    from scheduler.flow import choose_slot, contexts

    s, sid = ctx
    identity = start(ctx, "理发")
    review = flexible(ctx)["slot_review"]
    p = choose_slot(s, sid, review["id"], review["candidates"][0]["id"])
    with s.calendar.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_write BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    assert s.confirm(sid, p["id"], True)["status"] == "error"
    assert s.calendar.search() == []
    assert next(c for c in contexts(s, sid) if c["id"] == identity)["status"] == "failed"


def test_frontend_context_and_late_chat_response_guard():
    import subprocess

    result = subprocess.run(["node", "tests/ui_context_check.cjs"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cancel_other_task_cannot_revive_failed_correction(ctx):
    from scheduler.provider import ModelError

    s, sid = ctx
    other = start(ctx, "其他任务")
    draft_email(ctx)
    meeting = start(ctx, "会议")
    Tools(*ctx).execute(
        "propose_create", dict(title="会议", start=at("20", "09:00"), end=at("20", "10:00"))
    )
    bad = Agent(
        s,
        ScriptedModel(call("task_context", action="continue", task_id=meeting), ModelError("超时")),
        Settings(),
    )
    assert asyncio.run(bad.run(sid, "改到十点"))["status"] == "error" and s.pending(sid) is None
    cancel = Agent(
        s, ScriptedModel(call("task_context", action="cancel", task_id=other)), Settings()
    )
    assert "已取消" in asyncio.run(cancel.run(sid, "取消其他任务"))["message"]
    assert s.pending(sid) is None and s.calendar.search() == []


def test_flexible_model_cannot_select_first_slot_without_user(ctx):
    s, sid = ctx
    start(ctx, "理发")
    review = flexible(ctx)["slot_review"]
    result = Tools(*ctx).execute(
        "propose_create",
        dict(
            title="理发", start=review["candidates"][0]["start"], end=review["candidates"][0]["end"]
        ),
    )
    assert result.get("error") == "slot_selection_required"
    assert s.pending(sid) is None and s.calendar.search() == []


def test_resumed_proposal_still_rechecks_calendar_conflicts(ctx):
    from scheduler.flow import contexts

    s, sid = ctx
    old = start(ctx, "会议")
    proposal = Tools(*ctx).execute(
        "propose_create", dict(title="会议", start=at("20", "09:00"), end=at("20", "10:00"))
    )["proposal"]
    start(ctx, "另一个任务")
    s.calendar.create(dict(title="新占用", start=at("20", "09:00"), end=at("20", "10:00")))
    assert control(ctx, "resume", task_id=old).get("context")
    receipt = s.confirm(sid, proposal["id"], True)
    assert receipt["status"] == "error" and len(s.calendar.search()) == 1
    assert next(c for c in contexts(s, sid) if c["id"] == old)["status"] == "failed"


def test_cross_day_lower_bound_and_invalid_reminder_leave_state_unchanged(ctx):
    s, sid = ctx
    start(ctx, "理发")
    snapshot = s.session(sid)["state"]
    result = flexible(ctx, not_before=at("19", "15:00"))
    assert result.get("error") == "invalid_slot_range" and s.session(sid)["state"] == snapshot
    result = control(ctx, title="提醒", entity="reminder", reminder_at="bad")
    assert result.get("error") == "invalid_datetime" and s.session(sid)["state"] == snapshot

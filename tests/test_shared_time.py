"""Structured tool/SQLite regressions. Scripted fixtures are NOT NL parsing tests."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scheduler.agent import Agent
from scheduler.config import Settings
from scheduler.storage import Store
from scheduler.tools import Tools
from tests.helpers import ScriptedModel, call

NOW = datetime(2026, 10, 17, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def at(t):
    return f"2026-10-18T{t}:00+08:00"


@pytest.fixture
def ctx(tmp_path):
    store = Store(tmp_path / "synthetic.sqlite3", "Asia/Shanghai")
    return store, store.new_session()


def window():
    return dict(
        window_id="morning", intent_ids=["grocery", "breakfast"], start=at("09:00"), end=at("10:00")
    )


def pair():
    return [
        dict(intent_id="grocery", event=dict(title="买菜")),
        dict(intent_id="breakfast", event=dict(title="吃早餐")),
    ]


def test_logged_empty_extraction_returns_to_model_not_user(ctx):
    store, sid = ctx
    # Exact failing wire payload from the user's persisted session.
    raw = dict(
        intents=[dict(intent_id="grocery", version=None), dict(intent_id="breakfast", version=None)]
    )
    result = Tools(*ctx).execute("stage_task", raw)
    assert result.get("error") == "invalid_arguments"
    assert "event" in result["hint"]
    assert "question" not in result
    assert not store.session(sid)["state"].get("task")


@pytest.mark.parametrize(
    "text,kind",
    [
        ("明天早晨9-10点去买菜、吃早餐", "shared"),
        ("明天9点买菜，10点吃早餐", "separate"),
        ("明天9点到10点先买菜再吃早餐", "sequential"),
        ("明天9点到10点买菜，同时吃早餐", "parallel"),
        ("明天11点至16点家里大扫除，12点去寄快递，花15分钟", "nested"),
    ],
)
def test_five_structured_interpretations_preserve_only_known_fields(ctx, text, kind):
    store, sid = ctx
    args = dict(intents=pair())
    if kind in ("shared", "sequential", "parallel"):
        args["time_windows"] = [window()]
    if kind == "separate":
        args["intents"][0]["event"]["start"] = at("09:00")
        args["intents"][1]["event"]["start"] = at("10:00")
    if kind == "sequential":
        args["relations"] = [
            dict(first="grocery", second="breakfast", kind="sequential", confirmed=True)
        ]
    if kind == "parallel":
        args["relations"] = [
            dict(first="grocery", second="breakfast", kind="parallel", confirmed=True)
        ]
    if kind == "nested":
        args["intents"] = [
            dict(
                intent_id="clean",
                event=dict(title="大扫除", start=at("11:00"), end=at("16:00"), location="家里"),
            ),
            dict(
                intent_id="courier",
                event=dict(title="寄快递", start=at("12:00")),
                duration_minutes=15,
            ),
        ]
    model = ScriptedModel(call("stage_task", **args))
    result = asyncio.run(Agent(store, model, Settings(), now=lambda: NOW).run(sid, text))
    assert result["status"] == "clarification", result
    task = store.session(sid)["state"]["task"]
    assert len(task["intents"]) == 2
    assert store.calendar.search() == [] and store.pending(sid) is None
    if kind in ("shared", "sequential", "parallel"):
        assert task["time_windows"] == [window()]
        assert "09:00" in result["message"] and "10:00" in result["message"]
        assert (
            "标题" not in result["message"]
            and "买菜" in result["message"]
            and "吃早餐" in result["message"]
        )
        if kind != "parallel":
            assert all(
                "start" not in i["event"] and "end" not in i["event"] for i in task["intents"]
            )
            assert "分配" in result["message"] or "分界" in result["message"]
        else:
            assert "往返" in result["message"] or "注意力" in result["message"]
    elif kind == "separate":
        assert [i["event"]["start"] for i in task["intents"]] == [at("09:00"), at("10:00")]
        assert all("end" not in i["event"] for i in task["intents"])
        assert "开始时间" not in result["message"] and "标题" not in result["message"]
    else:
        assert task["intents"][1]["event"]["end"] == at("12:15")
        assert task["intents"][1]["duration_minutes"] == 15
        assert task["intents"][1]["event"].get("location", "") == ""
        assert "时长" not in result["message"]


def test_window_restart_targeted_completion_and_atomic_confirm(ctx):
    store, sid = ctx
    first = Tools(*ctx).execute(
        "stage_task",
        dict(
            intents=pair(),
            time_windows=[window()],
            relations=[
                dict(first="grocery", second="breakfast", kind="sequential", confirmed=True)
            ],
        ),
    )
    assert first.get("question")
    store = Store(store.calendar.path, "Asia/Shanghai")
    second = Tools(store, sid).execute(
        "stage_task",
        dict(
            intents=[
                dict(intent_id="grocery", event=dict(start=at("09:00"), end=at("09:40"))),
                dict(intent_id="breakfast", event=dict(start=at("09:40"), end=at("10:00"))),
            ]
        ),
    )
    assert second.get("proposal"), second
    assert second["proposal"]["event"]["task"]["time_windows"] == [window()]
    assert store.calendar.search() == []
    result = store.confirm(sid, second["proposal"]["id"], True)
    assert result["status"] == "committed"
    assert len(store.calendar.search()) == 2


def test_shared_bounds_not_dropped_by_alternative_resolution(ctx):
    from scheduler.tasks import validate_task
    from scheduler.calendar import CalendarError

    store, sid = ctx
    draft = Tools(*ctx).execute("stage_task", dict(intents=pair(), time_windows=[window()]))
    assert draft.get("task"), draft
    from copy import deepcopy

    task = deepcopy(draft["task"])
    for i, times in zip(task["intents"], [("09:00", "09:30"), ("09:30", "10:00")], strict=True):
        i["event"].update(start=at(times[0]), end=at(times[1]))
    changes = [
        dict(action="create", intent_id=i["intent_id"], event=i["event"]) for i in task["intents"]
    ]
    changes[0]["event"] = changes[0]["event"] | dict(start=at("08:00"), end=at("08:30"))
    with pytest.raises(CalendarError, match="outside_shared_window"):
        validate_task(store, task, changes)


def test_empty_tool_result_is_corrected_within_agent_turn(ctx):
    store, sid = ctx
    model = ScriptedModel(
        call(
            "stage_task",
            intents=[
                {"intent_id": "grocery", "version": None},
                {"intent_id": "breakfast", "version": None},
            ],
        ),
        call("stage_task", intents=pair(), time_windows=[window()]),
    )
    reply = asyncio.run(
        Agent(store, model, Settings(), now=lambda: NOW).run(sid, "明天早晨9-10点去买菜、吃早餐")
    )
    assert reply["status"] == "clarification"
    assert len(model.messages) == 2
    assert reply["trace"][0]["result"]["error"] == "invalid_arguments"
    assert "标题" not in reply["message"]
    assert len(store.session(sid)["state"]["task"]["intents"]) == 2


def test_duration_only_change_cannot_bypass_fixed_constraints(ctx):
    store, sid = ctx
    tools = Tools(*ctx)
    first = tools.execute(
        "stage_task",
        {
            "intents": [
                {
                    "intent_id": "a",
                    "event": {"title": "固定会议", "start": at("09:00"), "flexibility": "fixed"},
                    "duration_minutes": 30,
                }
            ]
        },
    )
    assert first.get("proposal")
    original = store.session(sid)["state"]["task"]
    result = Tools(*ctx).execute(
        "stage_task",
        {"intents": [{"intent_id": "a", "event": {"title": "固定会议"}, "duration_minutes": 60}]},
    )
    assert result.get("error") == "hard_constraint"
    assert store.session(sid)["state"]["task"] == original
    assert store.pending(sid)["id"] == first["proposal"]["id"]


def test_duration_recomputes_end_after_start_update(ctx):
    result = Tools(*ctx).execute(
        "stage_task",
        {
            "intents": [
                {
                    "intent_id": "a",
                    "event": {"title": "A", "start": at("09:00")},
                    "duration_minutes": 30,
                }
            ]
        },
    )
    assert result.get("proposal")
    moved = Tools(*ctx).execute(
        "stage_task", {"intents": [{"intent_id": "a", "event": {"start": at("10:00")}}]}
    )
    assert moved.get("proposal"), moved
    assert moved["proposal"]["event"]["task"]["intents"][0]["event"]["end"] == at("10:30")


def test_parallel_wire_keeps_tentative_times_but_requests_confirmation(ctx):
    # Shape captured from the real-model parallel scenario, replayed without an LLM.
    data = pair()
    for intent in data:
        intent["event"].update(start=at("09:00"), end=at("10:00"))
    result = Tools(*ctx).execute(
        "stage_task",
        {
            "intents": data,
            "relations": [
                {"first": "grocery", "second": "breakfast", "kind": "parallel", "confirmed": True}
            ],
        },
    )
    assert "完整区间" in result["question"] and "起止时间" in result["question"]
    assert "往返" in result["question"] and "注意力" in result["question"]
    assert not result.get("proposal")
    task = ctx[0].session(ctx[1])["state"]["task"]
    assert all(
        not i["event"]["overlap_allowed"] and not i["event"]["coexistence_verified"]
        for i in task["intents"]
    )
    assert ctx[0].calendar.search() == []


def test_inconsistent_duration_and_window_validation_do_not_mutate_state(ctx):
    store, sid = ctx
    bad = Tools(*ctx).execute(
        "stage_task",
        {
            "intents": [
                {
                    "intent_id": "a",
                    "event": {"title": "A", "start": at("09:00"), "end": at("10:00")},
                    "duration_minutes": 15,
                }
            ]
        },
    )
    assert bad.get("error") == "inconsistent_duration"
    assert not store.session(sid)["state"].get("task")
    bad = Tools(*ctx).execute(
        "stage_task",
        {"intents": pair(), "time_windows": [window() | {"intent_ids": ["grocery", "missing"]}]},
    )
    assert bad.get("error") == "invalid_window"
    assert not store.session(sid)["state"].get("task")

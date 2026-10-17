"""Clarification card and two-turn confirmation; scripted model, no real LLM."""

import asyncio
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from scheduler.agent import Agent
from scheduler.app import create_app
from scheduler.config import Settings
from scheduler.storage import Store
from tests.helpers import ScriptedModel, call


def test_shared_range_has_clear_next_step_and_then_confirm(tmp_path):
    store = Store(tmp_path / "ux.sqlite3", "Asia/Shanghai")
    sid = store.new_session()
    window = dict(
        window_id="w",
        intent_ids=["a", "b"],
        start="2026-10-18T09:00:00+08:00",
        end="2026-10-18T10:00:00+08:00",
    )
    model = ScriptedModel(
        call(
            "stage_task",
            intents=[
                dict(intent_id="a", event=dict(title="买菜")),
                dict(intent_id="b", event=dict(title="吃早餐")),
            ],
            time_windows=[window],
        ),
        lambda _: call(
            "task_context", action="continue", task_id=store.session(sid)["state"]["context_id"]
        ),
        call(
            "stage_task",
            intents=[
                dict(intent_id="a", event=dict(start=window["start"]), duration_minutes=40),
                dict(
                    intent_id="b",
                    event=dict(start="2026-10-18T09:40:00+08:00"),
                    duration_minutes=20,
                ),
            ],
            relations=[dict(first="a", second="b", kind="sequential", confirmed=True)],
        ),
    )
    agent = Agent(
        store,
        model,
        Settings(),
        now=lambda: datetime(2026, 10, 17, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    first = asyncio.run(agent.run(sid, "明天早晨9-10点去买菜、吃早餐"))
    assert "尚未添加" in first["message"] and "确认" in first["message"]
    client = TestClient(create_app(Settings(database=str(store.calendar.path)), model))
    client.cookies.set("schedule_session", sid)
    state = client.get("/api/state").json()
    assert state["clarification"]["windows"][0]["titles"] == ["买菜", "吃早餐"]
    assert state["clarification"]["windows"][0]["start"] == window["start"]
    assert state["proposal"] is None and state["events"] == []
    # Reload keeps the same next step; no write or default split.
    assert client.get("/api/state").json()["clarification"] == state["clarification"]
    second = asyncio.run(agent.run(sid, "先买菜40分钟，再吃早餐20分钟"))
    assert second["status"] == "proposal"
    state = client.get("/api/state").json()
    assert state["clarification"] is None
    assert len(state["proposal"]["event"]["preview"]) == 2 and state["events"] == []
    receipt = client.post(
        "/api/proposals/" + second["proposal"]["id"], json={"approve": True}
    ).json()
    assert receipt["status"] == "committed"
    assert [(e["title"], e["start"][11:16], e["end"][11:16]) for e in store.calendar.search()] == [
        ("买菜", "09:00", "09:40"),
        ("吃早餐", "09:40", "10:00"),
    ]


def test_clarification_card_renderer():
    result = subprocess.run(
        ["node", "tests/ui_clarification_check.cjs"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_known_duration_and_order_are_not_asked_again():
    import time
    from scheduler.tasks import clarification_view

    task = {
        "expires": time.time() + 600,
        "intents": [
            {"intent_id": "a", "event": {"title": "活动甲"}, "duration_minutes": 40},
            {"intent_id": "b", "event": {"title": "活动乙"}, "duration_minutes": 20},
        ],
        "relations": [{"first": "a", "second": "b", "kind": "sequential", "confirmed": True}],
        "time_windows": [
            {
                "window_id": "w",
                "intent_ids": ["a", "b"],
                "start": "2026-10-18T09:00:00+08:00",
                "end": "2026-10-18T10:00:00+08:00",
            }
        ],
    }
    view = clarification_view(task)
    question = view["windows"][0]["question"]
    assert "各需要多久" not in question and "先做哪项" not in question
    assert "分界" in question
    task["expires"] = 0
    assert clarification_view(task) is None

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scheduler.agent import Agent
from scheduler.config import Settings
from scheduler.provider import ModelError
from scheduler.storage import Store
from tests.helpers import ScriptedModel, call
from tests.test_calendar import event


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "agent.sqlite3", "Asia/Shanghai")
    return store, store.new_session()


def run(store, sid, model, text="明天下午3点和老王开会一小时", **options):
    settings = Settings(database=str(store.calendar.path), **options)
    return asyncio.run(
        Agent(
            store,
            model,
            settings,
            now=lambda: datetime(2026, 10, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        ).run(sid, text)
    )


def test_create_proposal_then_confirm_real_database(setup):
    store, sid = setup
    model = ScriptedModel(call("propose_create", **event()))
    result = run(store, sid, model)
    assert result["status"] == "proposal"
    assert store.calendar.search() == []
    assert "2026-10-17" in model.messages[0][0]["content"]
    receipt = store.confirm(sid, result["proposal"]["id"], True)
    assert receipt["status"] == "committed"
    assert len(store.calendar.search()) == 1
    assert store.calendar.get(receipt["event"]["id"]) == receipt["event"]
    assert store.confirm(sid, result["proposal"]["id"], True) == receipt


def test_multiturn_clarification_persisted_across_restart(setup):
    store, sid = setup
    first = run(
        store,
        sid,
        ScriptedModel(call("ask_user", fields=["start", "duration"])),
        text="明天和老王开会",
    )
    assert first["status"] == "clarification"
    again = Store(store.calendar.path, "Asia/Shanghai")
    model = ScriptedModel(call("propose_create", **event()))
    second = run(again, sid, model, text="下午3点，一小时")
    assert second["status"] == "proposal"
    assert any("明天和老王开会" in str(m) for m in model.messages[0])


def test_query_then_update_uses_observed_version(setup):
    store, sid = setup
    saved = store.calendar.create(event())

    def propose(messages):
        found = json.loads(messages[-1]["content"])["events"][0]
        return call("propose_update", event_id=found["id"], patch={"location": "海底捞"})

    model = ScriptedModel(call("search_events", query="老王"), propose)
    result = run(store, sid, model, text="把和老王的会地点改成海底捞")
    assert [t["tool"] for t in result["trace"]] == ["search_events", "propose_update"]
    assert store.calendar.get(saved["id"])["location"] == "会议室A"
    receipt = store.confirm(sid, result["proposal"]["id"], True)
    assert receipt["event"]["location"] == "海底捞"


def test_ambiguous_target_is_not_guessed(setup):
    store, sid = setup
    a = store.calendar.create(event())
    store.calendar.create(
        {**event(), "start": "2026-10-19T15:00:00+08:00", "end": "2026-10-19T16:00:00+08:00"}
    )
    model = ScriptedModel(
        call("search_events", query="老王"),
        call("propose_delete", event_id=a["id"]),
        call("ask_user", fields=["start", "duration"]),
    )
    result = run(store, sid, model, text="取消和老王的会")
    assert result["status"] == "clarification"
    assert result["trace"][1]["result"]["error"] == "ambiguous"
    assert len(result["candidates"]) == 2
    assert len(store.calendar.search()) == 2
    store.select(sid, a["id"])
    result = run(
        store, sid, ScriptedModel(call("propose_delete", event_id=a["id"])), text="删掉我选的这条"
    )
    receipt = store.confirm(sid, result["proposal"]["id"], True)
    assert receipt["event"]["id"] == a["id"]
    assert len(store.calendar.search()) == 1


def test_unobserved_id_cannot_be_deleted(setup):
    store, sid = setup
    saved = store.calendar.create(event())
    result = run(
        store,
        sid,
        ScriptedModel(
            call("propose_delete", event_id=saved["id"]),
            call("ask_user", fields=["start", "duration"]),
        ),
    )
    assert result["trace"][0]["result"]["error"] == "not_observed"
    assert store.calendar.get(saved["id"])


def test_conflict_feedback_and_llm_alternative_selection(setup):
    store, sid = setup
    store.calendar.create(event())
    model = ScriptedModel(
        call("propose_create", **{**event(), "title": "写周报"}),
        call(
            "find_slots",
            start=event()["start"],
            end="2026-10-18T19:00:00+08:00",
            duration_minutes=60,
        ),
        call("ask_user", fields=["start", "duration"]),
    )
    result = run(store, sid, model)
    assert result["trace"][0]["result"]["error"] == "conflict"
    assert result["trace"][1]["result"]["slots"][0]["start"].startswith("2026-10-18T16:00")
    assert len(store.calendar.search()) == 1
    assert "conflict" in model.messages[1][-1]["content"]


def test_model_claimed_success_is_discarded(setup):
    store, sid = setup
    result = run(store, sid, ScriptedModel({"role": "assistant", "content": "已删除所有日程。"}))
    assert "已删除" not in result["message"]
    assert result["status"] == "idle"


def test_loop_bound_and_invalid_tool_arguments(setup):
    store, sid = setup
    malformed = call("search_events")
    malformed["tool_calls"][0]["function"]["arguments"] = "{oops"
    result = run(
        store,
        sid,
        ScriptedModel(malformed, call("not_a_tool"), call("search_events")),
        max_iterations=3,
    )
    assert result["status"] == "error"
    assert len(result["trace"]) == 3
    assert result["trace"][0]["result"]["error"] == "invalid_arguments"
    assert result["trace"][1]["result"]["error"] == "unknown_tool"


def test_timeout_and_provider_failure_safe(setup):
    store, sid = setup
    result = run(store, sid, ScriptedModel(ModelError("模型超时")))
    assert result["status"] == "error"
    assert store.calendar.search() == []

    class Slow:
        async def complete(self, messages, tools):
            await asyncio.sleep(1)

    result = run(store, sid, Slow(), turn_timeout_seconds=0.1)
    assert result["status"] == "error"
    assert "超时" in result["message"]


def test_query_reply_grounded_in_results(setup):
    store, sid = setup
    store.calendar.create(event())
    result = run(
        store,
        sid,
        ScriptedModel(
            call("search_events", query="老汪"), {"role": "assistant", "content": "已删除所有日程"}
        ),
    )
    assert result["status"] == "query"
    assert "和老王开会" in result["message"]
    assert "已删除" not in result["message"]

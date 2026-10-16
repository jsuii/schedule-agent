"""Safety regressions against real calendar / proposal code; model is scripted."""

import asyncio
import json

import httpx
import pytest

from scheduler.agent import Agent
from scheduler.calendar import CalendarError
from scheduler.config import Settings
from scheduler.provider import CompatibleModel, ModelError
from scheduler.storage import Store
from tests.helpers import ScriptedModel, call
from tests.test_calendar import event


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "safe.sqlite3", "Asia/Shanghai")


def test_stale_update_does_not_overwrite_newer_values(store):
    saved = store.calendar.create(event())
    sid = store.new_session()
    model = ScriptedModel(
        call("search_events", query="老王"),
        call("propose_update", event_id=saved["id"], patch={"location": "旧提案"}),
    )
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "改地点"))
    store.calendar.update(saved["id"], 1, {"location": "外部新改动"})
    receipt = store.confirm(sid, result["proposal"]["id"], True)
    assert receipt["error"] == "stale"
    assert store.calendar.get(saved["id"])["location"] == "外部新改动"


def test_update_start_preserves_duration_and_other_fields(store):
    saved = store.calendar.create(event())
    sid = store.new_session()
    model = ScriptedModel(
        call("search_events", query="老王"),
        call("propose_update", event_id=saved["id"], patch={"start": "2026-10-19T10:00:00+08:00"}),
    )
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "移到后天10点"))
    receipt = store.confirm(sid, result["proposal"]["id"], True)
    assert receipt["event"]["end"] == "2026-10-19T11:00:00+08:00"
    assert receipt["event"]["location"] == "会议室A"


def test_batch_stops_after_first_terminal_tool(store):
    sid = store.new_session()
    first = call("ask_user", fields=["start", "duration"])
    first["tool_calls"].extend(call("propose_create", **event())["tool_calls"])
    result = asyncio.run(Agent(store, ScriptedModel(first), Settings()).run(sid, "安排"))
    assert result["status"] == "clarification"
    assert result["trace"][1]["result"]["error"] == "turn_finished"
    assert store.pending(sid) is None
    assert store.calendar.search() == []


def test_malformed_fields_cannot_be_written(store):
    sid = store.new_session()
    model = ScriptedModel(
        call("propose_create", **{**event(), "start": "2026-02-30T15:00:00+08:00"}),
        call("ask_user", fields=["start", "duration"]),
    )
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "错误日期"))
    assert result["trace"][0]["result"]["error"] == "invalid_datetime"
    assert store.pending(sid) is None


def test_selected_id_must_come_from_session_candidates(store):
    with pytest.raises(CalendarError, match="not_observed"):
        store.select(store.new_session(), "invented")


def test_request_cache_durable_and_incomplete_requests_do_not_repeat(store):
    sid = store.new_session()
    assert store.claim_request(sid, "one") is None
    assert store.claim_request(sid, "one")["status"] == "busy"
    result = {"status": "query", "message": "fixture"}
    store.finish_request(sid, "one", result)
    assert Store(store.calendar.path, "Asia/Shanghai").claim_request(sid, "one") == result


def test_tool_schema_is_json_and_unknown_fields_are_rejected(store):
    from scheduler.tools import SCHEMAS, Tools

    assert len(json.loads(json.dumps(SCHEMAS))) == 6
    sid = store.new_session()
    assert (
        Tools(store, sid).execute("propose_create", {**event(), "confirmed": True})["error"]
        == "invalid_arguments"
    )
    assert (
        Tools(store, sid).execute("propose_update", {"event_id": "x", "patch": {}})["error"]
        == "invalid_arguments"
    )


def test_provider_transport_failure_and_invalid_urls():
    async def run():
        def fail(req):
            raise httpx.ConnectError("private-credential")

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            with pytest.raises(ModelError, match="连接"):
                await CompatibleModel(Settings(model="local"), client).complete([], [])

    asyncio.run(run())
    for endpoint in [
        "http://remote.example/v1",
        "ftp://localhost",
        "https://key:secret@host/v1",
        "https://host/v1?api_key=secret",
    ]:
        with pytest.raises(ValueError):
            Settings(endpoint=endpoint)


def test_clarification_cannot_smuggle_false_success(store):
    sid = store.new_session()
    store.calendar.create(event())
    model = ScriptedModel(
        call("ask_user", question="已删除所有日程。还有什么安排？"),
        call("ask_user", fields=["start", "duration"]),
    )
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "安排会议"))
    assert "已删除" not in result["message"]
    assert result["status"] == "clarification"
    assert result["trace"][0]["result"]["error"] == "invalid_arguments"
    assert len(store.calendar.search()) == 1


def test_slot_query_shows_actual_times_and_empty_range(store):
    sid = store.new_session()
    model = ScriptedModel(
        call("find_slots", start=event()["start"], end=event()["end"], duration_minutes=60),
        {"role": "assistant", "content": None},
    )
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "查空档"))
    assert "2026-10-18 15:00" in result["message"]
    store.calendar.create(event())
    model = ScriptedModel(
        call("find_slots", start=event()["start"], end=event()["end"], duration_minutes=60),
        {"role": "assistant", "content": None},
    )
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "查空档"))
    assert "没有找到" in result["message"]


def test_confirm_clears_stale_candidate_cards(store):
    sid = store.new_session()
    saved = store.calendar.create(event())
    model = ScriptedModel(
        call("search_events", query="老王"), call("propose_delete", event_id=saved["id"])
    )
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "取消"))
    store.confirm(sid, result["proposal"]["id"], True)
    assert store.session(sid)["state"] == {}


def test_invalid_tool_feedback_identifies_fields_without_echoing_values(store):
    from scheduler.tools import Tools

    result = Tools(store, store.new_session()).execute(
        "propose_update",
        {"event_id": "synthetic-id", "patch": {"unknown_secret_field": "sensitive-fixture-value"}},
    )
    assert result["error"] == "invalid_arguments"
    assert result["issues"] == [{"path": "patch.unknown_secret_field", "type": "extra_forbidden"}]
    assert "sensitive-fixture-value" not in json.dumps(result)


def test_repeated_same_tool_error_stops_before_exhausting_model_budget(store):
    sid = store.new_session()
    bad = call("propose_update", event_id="not-observed", patch={"location": "测试室B"})
    model = ScriptedModel(bad, bad, bad, bad, bad)
    result = asyncio.run(Agent(store, model, Settings(max_iterations=8)).run(sid, "改地点"))
    assert result["status"] == "error"
    assert len(model.messages) == 3
    assert len(result["trace"]) == 3
    assert "连续" in result["message"]
    assert store.calendar.search() == []

"""Observed interaction failures: deterministic regression fixtures, no real LLM."""

import asyncio

from fastapi.testclient import TestClient

from scheduler.agent import Agent
from scheduler.app import create_app
from scheduler.config import Settings
from scheduler.storage import Store
from scheduler.tools import Tools
from tests.helpers import ScriptedModel, call
from tests.test_calendar import event


def fixture(tmp_path):
    store = Store(tmp_path / "sprint.sqlite3", "Asia/Shanghai")
    a = store.calendar.create(event())
    b = store.calendar.create(
        {**event(), "start": "2026-10-19T15:00:00+08:00", "end": "2026-10-19T16:00:00+08:00"}
    )
    sid = store.new_session()
    return store, sid, a, b


def test_user_selection_survives_model_refresh_search(tmp_path):
    store, sid, a, b = fixture(tmp_path)
    Tools(store, sid).execute("search_events", {"query": "老王"})
    store.select(sid, a["id"])
    model = ScriptedModel(
        call("search_events", query="老王"), call("propose_delete", event_id=a["id"])
    )
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "请继续处理我选择的这条日程"))
    assert result["status"] == "proposal"
    assert result["proposal"]["before"]["id"] == a["id"]
    assert store.calendar.get(a["id"]) and store.calendar.get(b["id"])


def test_selected_target_does_not_leak_into_unrelated_turn(tmp_path):
    store, sid, a, b = fixture(tmp_path)
    Tools(store, sid).execute("search_events", {"query": "老王"})
    store.select(sid, a["id"])
    agent = Agent(store, ScriptedModel(call("ask_user", fields=["date"])), Settings())
    asyncio.run(agent.run(sid, "先别取消，我要安排另一件事"))
    model = ScriptedModel(
        call("propose_delete", event_id=a["id"]), call("ask_user", fields=["selection"])
    )
    result = asyncio.run(Agent(store, model, Settings()).run(sid, "取消和老王的会议"))
    assert result["status"] == "clarification"
    assert result["trace"][0]["result"]["error"] == "ambiguous"
    assert store.calendar.get(a["id"])


def test_false_success_is_not_visible_after_page_refresh(tmp_path):
    app = create_app(
        Settings(database=str(tmp_path / "web.sqlite3")),
        ScriptedModel({"role": "assistant", "content": "已删除所有日程。"}),
    )
    client = TestClient(app)
    result = client.post("/api/chat", json={"text": "帮我看看安排", "request_id": "one"}).json()
    assert "已删除" not in result["message"]
    state = client.get("/api/state").json()
    assert not any("已删除" in m["content"] for m in state["history"])


def test_running_model_is_visible_without_credentials(tmp_path):
    app = create_app(
        Settings(
            database=str(tmp_path / "web.sqlite3"),
            model="deepseek/deepseek-v4.1-flash",
            api_key="synthetic-secret",
        ),
        ScriptedModel(),
    )
    client = TestClient(app)
    state = client.get("/api/state").json()
    assert state["model"] == "deepseek/deepseek-v4.1-flash"
    assert "synthetic-secret" not in str(state)
    assert client.get("/api/health").json()["model"] == state["model"]


def test_new_matching_event_requires_fresh_disambiguation(tmp_path):
    store = Store(tmp_path / "new-match.sqlite3", "Asia/Shanghai")
    a = store.calendar.create(event())
    sid = store.new_session()
    tools = Tools(store, sid)
    tools.execute("search_events", {"query": "老王"})
    store.calendar.create(
        {**event(), "start": "2026-10-19T15:00:00+08:00", "end": "2026-10-19T16:00:00+08:00"}
    )
    result = tools.execute("propose_delete", {"event_id": a["id"]})
    assert result["error"] == "stale_candidates"
    assert store.pending(sid) is None
    assert len(store.calendar.search()) == 2


def test_explicit_selection_from_truncated_results_can_be_proposed(tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    store = Store(tmp_path / "large.sqlite3", "Asia/Shanghai")
    base = datetime(2026, 10, 18, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    for i in range(51):
        start = base + timedelta(days=i)
        store.calendar.create(
            {
                "title": "老王会议",
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
            }
        )
    sid = store.new_session()
    found = Tools(store, sid).execute("search_events", {"query": "老王"})
    assert found["truncated"] is True
    selected = found["events"][0]["id"]
    store.select(sid, selected)
    result = Tools(store, sid).execute("propose_delete", {"event_id": selected})
    assert result.get("proposal"), result


def test_confirmation_verification_failure_rolls_back_the_write(tmp_path, monkeypatch):
    store = Store(tmp_path / "rollback.sqlite3", "Asia/Shanghai")
    sid = store.new_session()
    proposal = store.propose(sid, "create", event())
    # Simulate a failed state verification after INSERT, not a failed model call.
    monkeypatch.setattr(store.calendar, "get", lambda *args, **kwargs: None)
    receipt = store.confirm(sid, proposal["id"], True)
    assert receipt["status"] == "error"
    assert receipt["error"] == "verification_failed"
    assert store.calendar.search() == []
    assert store.confirm(sid, proposal["id"], True) == receipt


def test_update_readback_failure_restores_original_record(tmp_path, monkeypatch):
    store = Store(tmp_path / "update-rollback.sqlite3", "Asia/Shanghai")
    saved = store.calendar.create(event())
    sid = store.new_session()
    proposal = store.propose(sid, "update", {**event(), "location": "B"}, saved)
    get = store.calendar.get
    calls = 0

    def failed_readback(*args, **kwargs):
        nonlocal calls
        calls += 1
        return get(*args, **kwargs) if calls == 1 else saved

    monkeypatch.setattr(store.calendar, "get", failed_readback)
    result = store.confirm(sid, proposal["id"], True)
    assert result["error"] == "verification_failed"
    monkeypatch.undo()
    assert store.calendar.get(saved["id"]) == saved


def test_delete_verification_failure_restores_deleted_record(tmp_path, monkeypatch):
    store = Store(tmp_path / "delete-rollback.sqlite3", "Asia/Shanghai")
    saved = store.calendar.create(event())
    sid = store.new_session()
    proposal = store.propose(sid, "delete", event(), saved)
    monkeypatch.setattr(store.calendar, "get", lambda *args, **kwargs: saved)
    result = store.confirm(sid, proposal["id"], True)
    assert result["error"] == "verification_failed"
    monkeypatch.undo()
    assert store.calendar.get(saved["id"]) == saved


def test_alternative_question_cannot_claim_no_slots_without_a_search(tmp_path):
    store, sid, _, _ = fixture(tmp_path)
    toolset = Tools(store, sid)
    toolset.execute("search_events", {"query": "老王"})
    result = toolset.execute("ask_user", {"fields": ["alternative"]})
    assert result["error"] == "missing_observation"
    assert "question" not in result


def test_empty_slot_search_can_truthfully_report_no_availability(tmp_path):
    store, sid, a, _ = fixture(tmp_path)
    toolset = Tools(store, sid)
    assert (
        toolset.execute(
            "find_slots", {"start": a["start"], "end": a["end"], "duration_minutes": 60}
        )["slots"]
        == []
    )
    result = toolset.execute("ask_user", {"fields": ["alternative"]})
    assert "没有找到可用时段" in result["question"]


def test_updated_into_multiple_conflicts_keeps_original_event(tmp_path):
    from scheduler.calendar import CalendarError
    import pytest

    store = Store(tmp_path / "conflicts.sqlite3", "Asia/Shanghai")
    original = store.calendar.create(event())
    store.calendar.create(
        {
            **event(),
            "title": "冲突A",
            "start": "2026-10-18T17:00:00+08:00",
            "end": "2026-10-18T18:00:00+08:00",
        }
    )
    store.calendar.create(
        {
            **event(),
            "title": "冲突B",
            "start": "2026-10-18T18:00:00+08:00",
            "end": "2026-10-18T19:00:00+08:00",
        }
    )
    with pytest.raises(CalendarError) as error:
        store.calendar.update(
            original["id"],
            1,
            {"start": "2026-10-18T17:30:00+08:00", "end": "2026-10-18T18:30:00+08:00"},
        )
    assert error.value.code == "conflict"
    assert len(error.value.details["events"]) == 2
    assert store.calendar.get(original["id"]) == original


def test_partial_overlap_and_midnight_timezone_query(tmp_path):
    from scheduler.calendar import CalendarError
    import pytest

    store = Store(tmp_path / "midnight.sqlite3", "Asia/Shanghai")
    saved = store.calendar.create(
        {
            "title": "跨午夜合成工作",
            "start": "2026-10-17T23:30:00+08:00",
            "end": "2026-10-18T00:30:00+08:00",
        }
    )
    assert (
        store.calendar.search(start="2026-10-18T00:00:00+08:00", end="2026-10-19T00:00:00+08:00")[
            0
        ]["id"]
        == saved["id"]
    )
    with pytest.raises(CalendarError, match="conflict"):
        store.calendar.create(
            {
                "title": "部分重叠",
                "start": "2026-10-18T00:00:00+08:00",
                "end": "2026-10-18T01:00:00+08:00",
            }
        )
    adjacent = store.calendar.create(
        {"title": "相邻", "start": "2026-10-18T00:30:00+08:00", "end": "2026-10-18T01:00:00+08:00"}
    )
    assert adjacent["id"] != saved["id"]


def test_correction_supersedes_pending_proposal_without_writing(tmp_path):
    from scheduler.calendar import CalendarError
    import pytest

    store = Store(tmp_path / "correction.sqlite3", "Asia/Shanghai")
    sid = store.new_session()
    first = asyncio.run(
        Agent(store, ScriptedModel(call("propose_create", **event())), Settings()).run(
            sid, "明天下午3点和老王开会一小时"
        )
    )
    updated = {**event(), "start": "2026-10-18T16:00:00+08:00", "end": "2026-10-18T17:00:00+08:00"}
    model = ScriptedModel(call("propose_create", **updated))
    corrected = asyncio.run(Agent(store, model, Settings()).run(sid, "改到下午4点，其他不变"))
    from scheduler.calendar import EventInput

    assert corrected["proposal"]["event"] == EventInput(**updated).model_dump()
    assert any("明天下午3点" in str(m) for m in model.messages[0])
    with pytest.raises(CalendarError, match="proposal_expired"):
        store.confirm(sid, first["proposal"]["id"], True)
    assert store.calendar.search() == []
    assert (
        store.confirm(sid, corrected["proposal"]["id"], True)["event"]["start"] == updated["start"]
    )

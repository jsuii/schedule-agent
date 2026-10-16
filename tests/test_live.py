"""OPT-IN real provider evaluation with synthetic data only. Not run in offline delivery."""

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scheduler.agent import Agent
from scheduler.config import Settings
from scheduler.provider import CompatibleModel
from scheduler.storage import Store

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_LLM") != "1", reason="未启用真实模型联调；离线测试不调用 LLM"
    ),
]


def test_live_chinese_multiturn_and_create(tmp_path):
    settings = Settings.load()
    assert settings.model.strip(), "请先配置 .env endpoint/model/API key"
    store = Store(tmp_path / "live.sqlite3", settings.timezone)
    sid = store.new_session()
    agent = Agent(store, CompatibleModel(settings), settings)
    first = asyncio.run(agent.run(sid, "明天和测试联系人开会"))
    assert first["status"] == "clarification", first["message"]
    assert store.calendar.search() == []
    second = asyncio.run(agent.run(sid, "下午3点开始，持续1小时，地点测试室A"))
    assert second["status"] == "proposal", second["message"]
    wanted = (datetime.now(ZoneInfo(settings.timezone)) + timedelta(days=1)).date().isoformat()
    assert second["proposal"]["event"]["start"].startswith(wanted + "T15:00")
    receipt = store.confirm(sid, second["proposal"]["id"], True)
    assert receipt["status"] == "committed"
    assert store.calendar.get(receipt["event"]["id"]) == receipt["event"]


def test_live_fuzzy_reference_uses_tools_and_proposes_correct_change(tmp_path):
    settings = Settings.load()
    store = Store(tmp_path / "live.sqlite3", settings.timezone)
    tomorrow = (datetime.now(ZoneInfo(settings.timezone)) + timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    saved = store.calendar.create(
        {
            "title": "和测试老王讨论周报",
            "start": tomorrow.isoformat(),
            "end": (tomorrow + timedelta(hours=1)).isoformat(),
            "location": "会议室A",
        }
    )
    agent = Agent(store, CompatibleModel(settings), settings)
    result = asyncio.run(agent.run(store.new_session(), "把和测试老王的会地点改成会议室B"))
    assert result["status"] == "proposal", result["message"]
    assert result["proposal"]["before"]["id"] == saved["id"]
    assert result["proposal"]["event"]["location"] == "会议室B"
    assert [t["tool"] for t in result["trace"]].index("search_events") < [
        t["tool"] for t in result["trace"]
    ].index("propose_update")
    assert store.calendar.get(saved["id"])["location"] == "会议室A"

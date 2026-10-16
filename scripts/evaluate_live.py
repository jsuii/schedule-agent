"""Run explicitly authorized DeepSeek evaluations with persistent budgets and fixtures.

Run from project root: LLM_MODEL=deepseek/deepseek-v4.1-flash
LLM_ENDPOINT=https://openrouter.ai/api/v1 python -m scripts.evaluate_live SCENARIO
Reports/database are ignored under data/sprint-20261017; never use real calendars.
"""

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from scheduler.agent import Agent
from scheduler.config import Settings
from scheduler.provider import CompatibleModel
from scheduler.storage import Store
from scripts.live_support import ProbeTransport

ROOT = Path("data/sprint-20261017")


async def evaluate(name):
    settings = Settings.load()
    if (
        settings.model != "deepseek/deepseek-v4.1-flash"
        or settings.endpoint != "https://openrouter.ai/api/v1/chat/completions"
    ):
        raise SystemExit("必须使用明确的DeepSeek/OpenRouter环境覆盖；不会请求Gemini。")
    transport = ProbeTransport(
        ROOT / "report.json", settings.endpoint, secret=settings.api_key.get_secret_value()
    )
    if any(r["name"] == name for r in transport.report["scenarios"]):
        raise SystemExit("该场景已经记录；禁止无理由自动重复测试。")
    record = {"name": name, "model": settings.model, "turns": []}
    transport.report["scenarios"].append(record)
    transport.scenario = name
    before = len(transport.report["requests"])
    store = Store(ROOT / (name + "-" + uuid4().hex[:8] + ".sqlite3"), settings.timezone)
    sid = store.new_session()
    tomorrow = (datetime.now(ZoneInfo(settings.timezone)) + timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )

    def event(day=0, title="合成老王会议"):
        start = tomorrow + timedelta(days=day)
        return {
            "title": title,
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
            "location": "测试室A",
        }

    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False
    ) as client:
        agent = Agent(store, CompatibleModel(settings, client), settings)

        async def turn(text, expected, confirm=False):
            result = await agent.run(sid, text)
            record["turns"].append({"input": text, "result": result})
            transport.save()
            print(
                json.dumps(
                    {
                        "scenario": name,
                        "status": result["status"],
                        "tools": [
                            t["tool"]
                            + (":" + t["result"]["error"] if "error" in t["result"] else "")
                            for t in result["trace"]
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            assert result["status"] == expected, f"expected {expected}, got {result['status']}"
            if confirm:
                receipt = store.confirm(sid, result["proposal"]["id"], True)
                record["turns"][-1]["receipt"] = receipt
                assert receipt["status"] == "committed"
            transport.save()
            return result

        try:
            if name in ("historical_update", "update_after_fix"):
                await turn("明天下午3点和合成老王开会一小时，地点测试室A", "proposal", True)
                await turn("我明天有什么安排？", "query")
                await turn("把和合成老王的会地点改成测试室B", "proposal", True)
                assert store.calendar.search()[0]["location"] == "测试室B"
            elif name == "query":
                saved = store.calendar.create(event())
                result = await turn("明天和合成老王的会议是什么时间、什么地点？", "query")
                assert saved["id"] in [e["id"] for e in result["events"]]
            elif name == "clarification":
                await turn("帮我安排明天下午的合成项目会议。", "clarification")
                result = await turn("三点开始，一个小时，地点测试室A。", "proposal", True)
                assert result["proposal"]["event"]["start"] == tomorrow.isoformat()
            elif name == "ambiguity":
                a = store.calendar.create(event())
                b = store.calendar.create(event(1))
                result = await turn("取消和合成老王的会议。", "clarification")
                assert len(result["candidates"]) == 2 and len(store.calendar.search()) == 2
                store.select(sid, a["id"])
                await turn("请继续处理我选择的这条日程", "proposal", True)
                assert store.calendar.get(a["id"]) is None and store.calendar.get(b["id"])
            elif name in ("conflict", "conflict_after_fix"):
                store.calendar.create(event())
                result = await turn("明天下午3点安排合成周报工作，持续1小时。", "clarification")
                assert any(t["result"].get("error") == "conflict" for t in result["trace"]) or any(
                    "slots" in t["result"] for t in result["trace"]
                )
                assert len(store.calendar.search()) == 1
                result = await turn("那就改为明天下午4点开始，持续1小时。", "proposal", True)
                assert (
                    result["proposal"]["event"]["start"]
                    == (tomorrow + timedelta(hours=1)).isoformat()
                )
            else:
                raise ValueError("Unknown scenario")
            record["status"] = "passed"
        except Exception as exc:
            record["status"] = "failed"
            record["error_type"] = type(exc).__name__
            record["final_events"] = store.calendar.search()
        finally:
            record["requests"] = len(transport.report["requests"]) - before
            record["final_events"] = store.calendar.search()
            transport.save()
    print(
        json.dumps(
            {k: v for k, v in record.items() if k not in ["turns", "final_events"]},
            ensure_ascii=False,
        )
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenario",
        choices=[
            "historical_update",
            "update_after_fix",
            "query",
            "clarification",
            "ambiguity",
            "conflict",
            "conflict_after_fix",
        ],
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(args.scenario)))

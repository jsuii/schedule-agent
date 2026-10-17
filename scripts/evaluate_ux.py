"""Focused real-model checks. Synthetic data; fresh exclusive budget, no automatic retry."""

import argparse
import asyncio
from datetime import datetime
import fcntl
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import uvicorn

from scheduler.agent import Agent
from scheduler.app import create_app
from scheduler.config import Settings
from scheduler.provider import CompatibleModel
from scheduler.storage import Store
from scripts.live_support import ProbeTransport

ROOT = Path("data/ux-sprint-20261017")


async def main(name):
    settings = Settings.load()
    if (
        settings.model != "deepseek/deepseek-v4.1-flash"
        or settings.endpoint != "https://openrouter.ai/api/v1/chat/completions"
    ):
        raise SystemExit("Explicit DeepSeek/OpenRouter environment required.")
    probe = ProbeTransport(
        ROOT / "report.json",
        settings.endpoint,
        limit=20,
        cost_limit=0.04,
        secret=settings.api_key.get_secret_value(),
    )
    probe.scenario = name
    async with httpx.AsyncClient(
        transport=probe, trust_env=False, follow_redirects=False
    ) as client:
        model = CompatibleModel(settings, client)
        day = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
        if name == "browser":
            settings.database = str(ROOT / "browser.sqlite3")
            app = create_app(settings, model)
            await uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=8002, log_level="warning")
            ).serve()
            return
        if any(x["name"] == name for x in probe.report["scenarios"]):
            raise SystemExit("Already recorded; no automatic rerun.")
        store = Store(ROOT / (name + "-" + uuid4().hex[:8] + ".sqlite3"), settings.timezone)
        sid = store.new_session()
        agent = Agent(store, model, settings)
        record = {"name": name, "turns": []}
        probe.report["scenarios"].append(record)
        probe.save()

        async def turn(text):
            result = await agent.run(sid, text)
            record["turns"].append(
                {"input": text, "result": result, "state": store.session(sid)["state"]}
            )
            probe.save()
            print(
                json.dumps(
                    {
                        "scenario": name,
                        "status": result["status"],
                        "tools": [
                            x["tool"]
                            + (":" + x["result"]["error"] if "error" in x["result"] else "")
                            for x in result["trace"]
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return result

        def commit(result):
            assert result.get("proposal"), result["status"]
            receipt = store.confirm(sid, result["proposal"]["id"], True)
            record["receipt"] = receipt
            assert receipt["status"] == "committed"
            return store.calendar.search()

        try:
            if name == "exact":
                result = await turn("今天11点至16点家里大扫除，12点去寄个快递，花15分钟。")
                task = store.session(sid)["state"].get("task")
                assert task and len(task["intents"]) == 2
                events = [i["event"] for i in task["intents"]]
                assert events[0]["location"] in ("家", "家里", "家中") and not events[1].get(
                    "location"
                )
                assert events[1]["end"].endswith("12:15:00+08:00")
                assert result["status"] == "clarification"
                result = await turn("这两个活动可以并行，大扫除可以临时暂停。")
                assert len(store.session(sid)["state"]["task"]["intents"]) == 2
                assert result["status"] == "clarification"
                result = await turn(
                    "快递15分钟包含往返，大扫除暂停期间注意力没有冲突，我已核实。快递地点暂未填写，保留未知即可。"
                )
                events = commit(result)
                assert len(events) == 2 and not events[1]["location"]
            elif name == "missing":
                result = await turn("今天9点到10点在甲室做合成整理，11点在乙室做合成登记。")
                task = store.session(sid)["state"].get("task")
                assert result["status"] == "clarification" and task and len(task["intents"]) == 2
                result = await turn("合成登记持续15分钟，其他保持不变。")
                events = commit(result)
                assert (
                    len(events) == 2
                    and events[0]["location"] == "甲室"
                    and events[1]["location"] == "乙室"
                )
                assert events[1]["end"].endswith("11:15:00+08:00")
            elif name == "independent":
                events = commit(
                    await turn(
                        "今天9:00–10:30在甲室做合成整理，11:00–11:15在乙室做合成登记，请一起安排。"
                    )
                )
                assert (
                    len(events) == 2
                    and events[0]["end"].endswith("10:30:00+08:00")
                    and events[1]["end"].endswith("11:15:00+08:00")
                )
                assert events[0]["location"] == "甲室" and events[1]["location"] == "乙室"
            elif name == "hard":
                existing = store.calendar.create(
                    {
                        "title": "合成固定面试",
                        "start": day + "T11:00:00+08:00",
                        "end": day + "T12:00:00+08:00",
                        "flexibility": "fixed",
                        "interruptibility": "noninterruptible",
                    }
                )
                result = await turn(
                    "今天11:30到11:45去合成寄快递（可改时间），13:00到14:00在家做合成整理，请一起安排。"
                )
                case = result.get("conflict")
                assert case and case["plans"]
                chosen = store.choose_plan(sid, case["plans"][0]["id"])
                events = commit({"status": "proposal", "proposal": chosen})
                assert len(events) == 3 and store.calendar.get(existing["id"]) == existing
            record["outcome"] = "passed"
        except Exception as exc:
            record.update(outcome="failed", error=type(exc).__name__)
            raise
        finally:
            probe.save()
            rows = probe.report["requests"]
            print(
                json.dumps(
                    {
                        "requests": len(rows),
                        "tokens": sum(
                            (r.get("usage") or {}).get("total_tokens", 0) or 0 for r in rows
                        ),
                        "cost": sum((r.get("usage") or {}).get("cost", 0) or 0 for r in rows),
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["exact", "missing", "independent", "hard", "browser"])
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "evaluation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another evaluation owns this budget.") from None
        try:
            asyncio.run(main(parser.parse_args().scenario))
        except KeyboardInterrupt:
            pass

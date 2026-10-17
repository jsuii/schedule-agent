"""Opt-in synthetic conflict acceptance, shared persistent 30-request/$0.05 ledger.

Environment overrides required. Never retries a recorded scenario, never changes .env.
"""

import argparse
import asyncio
import json
import fcntl
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from scheduler.agent import Agent
from scheduler.app import create_app
from scheduler.config import Settings
from scheduler.provider import CompatibleModel
from scheduler.storage import Store
from scripts.live_support import ProbeTransport

ROOT = Path("data/conflict-sprint-20261017")


def event(title, start, end, **kw):
    return dict(
        title=title, start=f"2026-10-19T{start}:00+08:00", end=f"2026-10-19T{end}:00+08:00", **kw
    )


async def main(name):
    settings = Settings.load()
    if (
        settings.model != "deepseek/deepseek-v4.1-flash"
        or settings.endpoint != "https://openrouter.ai/api/v1/chat/completions"
    ):
        raise SystemExit("Explicit DeepSeek/OpenRouter environment required.")
    transport = ProbeTransport(
        ROOT / "report.json",
        settings.endpoint,
        limit=30,
        cost_limit=0.05,
        secret=settings.api_key.get_secret_value(),
    )
    transport.scenario = name
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False
    ) as client:
        model = CompatibleModel(settings, client)
        if name == "browser":
            settings.database = str(ROOT / "browser.sqlite3")
            app = create_app(settings, model)
            if not app.state.store.calendar.search():
                app.state.store.calendar.create(event("合成宠物检查", "13:00", "17:00"))
            await uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=8002, log_level="warning")
            ).serve()
            return
        if any(r["name"] == name for r in transport.report["scenarios"]):
            raise SystemExit("Scenario already recorded; no automatic rerun.")
        record = {"name": name, "turns": []}
        transport.report["scenarios"].append(record)
        transport.save()
        store = Store(ROOT / (name + "-" + uuid4().hex[:8] + ".sqlite3"), settings.timezone)
        sid = store.new_session()
        agent = Agent(store, model, settings)

        async def turn(text):
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
            return result

        try:
            if name == "pet_interview":
                old = store.calendar.create(event("合成宠物检查", "13:00", "17:00"))
                result = await turn(
                    "请安排2026年10月19日16:00到18:00的合成面试。先按此时间提出，不要擅自调整。"
                )
                assert result.get("conflict"), result["status"]
                assert len(store.calendar.search()) == 1
                result = await turn(
                    "面试保持16:00–18:00，合成宠物检查改为13:00–15:30。请将面试新增和宠物检查修改作为一次统一提案。"
                )
                assert result.get("proposal", {}).get("action") == "batch", result["status"]
                receipt = store.confirm(sid, result["proposal"]["id"], True)
                record["receipt"] = receipt
                assert receipt["status"] == "committed"
                assert len(store.calendar.search()) == 2
                assert store.calendar.get(old["id"])["end"].endswith("15:30:00+08:00")
            elif name == "pause":
                old = store.calendar.create(event("合成大扫除", "11:00", "16:00"))
                result = await turn("安排2026年10月19日12:00到12:30的合成寄快递。先按此时间提出。")
                assert result.get("conflict") and len(store.calendar.search()) == 1
                result = await turn(
                    "大扫除可暂停并恢复，两项活动都在家中，快递上门，无需通勤，我已核实注意力可兼顾并明确允许两项重叠。请更新大扫除的可暂停性、地点及双方重叠授权与核验属性，同时新增快递，保留两项时间。"
                )
                assert result.get("proposal", {}).get("action") == "batch", result["status"]
                receipt = store.confirm(sid, result["proposal"]["id"], True)
                record["receipt"] = receipt
                assert receipt["status"] == "committed"
                assert len(store.calendar.search()) == 2
                assert store.calendar.get(old["id"])["start"] == old["start"]
            elif name in ("fixed", "fixed_after_fix", "fixed_flexible", "fixed_after_guidance"):
                old = store.calendar.create(
                    event(
                        "合成固定面试",
                        "11:00",
                        "12:00",
                        flexibility="fixed",
                        interruptibility="noninterruptible",
                    )
                )
                result = await turn(
                    "请安排2026年10月19日11:30到12:00的合成寄快递，必须本人参加。快递时间可以移动，请先检查此时段并展示方案。"
                    if name in ("fixed_flexible", "fixed_after_guidance")
                    else "请安排2026年10月19日11:30到12:00的合成寄快递，必须本人参加。先按此时间提出。"
                )
                assert result.get("conflict")
                case = result["conflict"]
                assert case["facts"][0]["classification"] == "hard_conflict"
                assert case["plans"]
                chosen = store.choose_plan(sid, case["plans"][0]["id"])
                receipt = store.confirm(sid, chosen["id"], True)
                record["receipt"] = receipt
                assert receipt["status"] == "committed"
                assert store.calendar.get(old["id"]) == old
            record["outcome"] = "passed"
        except Exception as exc:
            record["outcome"] = "failed"
            record["failure"] = type(exc).__name__
            raise
        finally:
            transport.save()
            rows = transport.report["requests"]
            print(
                json.dumps(
                    {
                        "requests": len(rows),
                        "tokens": sum(
                            (r.get("usage") or {}).get("total_tokens", 0) or 0 for r in rows
                        ),
                        "cost": sum((r.get("usage") or {}).get("cost", 0) or 0 for r in rows),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenario",
        choices=[
            "pet_interview",
            "pause",
            "fixed",
            "fixed_after_fix",
            "fixed_flexible",
            "fixed_after_guidance",
            "browser",
        ],
    )
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        with (ROOT / "evaluation.lock").open("a") as evaluation_lock:
            try:
                fcntl.flock(evaluation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit(
                    "Another evaluator/browser owns this budget ledger. Stop it before running another scenario."
                ) from None
            asyncio.run(main(parser.parse_args().scenario))
    except KeyboardInterrupt:
        pass

"""Focused opt-in synthetic cancellation/recovery checks, exclusive bounded ledger."""

import argparse
import asyncio
from datetime import datetime, timedelta
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

ROOT = Path("data/trash-sprint-20261017")


def seed(store, day):
    for title, start, end in [
        ("合成面试", "10:00", "11:00"),
        ("合成羽毛球", "15:00", "16:00"),
        ("合成晚餐", "18:00", "19:00"),
    ]:
        store.calendar.create(
            {
                "title": title,
                "start": day + "T" + start + ":00+08:00",
                "end": day + "T" + end + ":00+08:00",
            }
        )


async def main(name):
    settings = Settings.load()
    if (
        settings.model != "deepseek/deepseek-v4.1-flash"
        or settings.endpoint != "https://openrouter.ai/api/v1/chat/completions"
    ):
        raise SystemExit("Explicit verified DeepSeek/OpenRouter environment required.")
    probe = ProbeTransport(
        ROOT / "report.json",
        settings.endpoint,
        limit=16,
        cost_limit=0.035,
        secret=settings.api_key.get_secret_value(),
    )
    probe.scenario = name
    async with httpx.AsyncClient(
        transport=probe, trust_env=False, follow_redirects=False
    ) as client:
        model = CompatibleModel(settings, client)
        now = datetime.now(ZoneInfo(settings.timezone))
        day = now.date().isoformat()
        if name == "browser":
            settings.database = str(ROOT / "browser.sqlite3")
            app = create_app(settings, model)
            if not app.state.store.calendar.search() and not app.state.store.calendar.trash():
                seed(app.state.store, day)
            await uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=8002, log_level="warning")
            ).serve()
            return
        if any(x["name"] == name for x in probe.report["scenarios"]):
            raise SystemExit("Already recorded; no automatic rerun.")
        store = Store(ROOT / (name + "-" + uuid4().hex[:8] + ".sqlite3"), settings.timezone)
        seed(store, day)
        sid = store.new_session()
        agent = Agent(store, model, settings)
        record = {"name": name, "turns": []}
        probe.report["scenarios"].append(record)
        probe.save()

        async def turn(text):
            result = await agent.run(sid, text)
            record["turns"].append({"input": text, "result": result})
            probe.save()
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

        def confirm(result):
            receipt = store.confirm(sid, result["proposal"]["id"], True)
            record["turns"][-1]["receipt"] = receipt
            assert receipt["status"] == "committed", receipt.get("error")
            return receipt

        try:
            if name in ("cancel_exclude_undo", "cancel_exclude_undo_after_schema"):
                result = await turn("取消今天的所有安排。")
                assert result.get("proposal", {}).get("action") == "cancel_batch"
                assert (
                    len(result["proposal"]["event"]["selected"]) == 3
                    and len(store.calendar.search()) == 3
                )
                result = await turn("保留下午的合成羽毛球，其他都取消。")
                assert len(result["proposal"]["event"]["selected"]) == 2
                assert "合成羽毛球" not in [
                    e["title"] for e in result["proposal"]["event"]["selected"]
                ]
                confirm(result)
                assert len(store.calendar.search()) == 1 and len(store.calendar.trash()) == 2
                result = await turn("撤销刚才的删除，恢复刚才删除的全部日程。")
                assert result.get("proposal", {}).get("action") == "restore_batch"
                confirm(result)
                assert len(store.calendar.search()) == 3 and not store.calendar.trash()
                result = await turn("查看回收站。")
                assert result.get("view") == "trash"
            elif name == "week":
                result = await turn("取消本周所有安排。")
                assert result.get("proposal", {}).get("action") == "cancel_batch"
                start = (now - timedelta(days=now.weekday())).date().isoformat()
                assert result["proposal"]["event"]["range"]["start"].startswith(start)
                assert len(store.calendar.search()) == 3
                confirm(result)
                assert not store.calendar.search() and len(store.calendar.trash()) == 3
            elif name == "recovery_query":
                from scheduler.trash import propose_cancel

                p = propose_cancel(store, sid, {"kind": "today"})
                store.confirm(sid, p["id"], True)
                yesterday = (now - timedelta(days=1)).replace(
                    hour=15, minute=0, second=0, microsecond=0
                )
                with store.calendar.connect() as conn:
                    conn.execute(
                        "UPDATE events SET deleted_at=? WHERE deleted_at IS NOT NULL",
                        (int(yesterday.timestamp()),),
                    )
                result = await turn("恢复昨天删除的合成羽毛球安排。")
                assert result.get("proposal", {}).get("action") == "restore_batch"
                assert [e["title"] for e in result["proposal"]["event"]["selected"]] == [
                    "合成羽毛球"
                ]
                confirm(result)
                assert len(store.calendar.search()) == 1 and len(store.calendar.trash()) == 2
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
    parser.add_argument(
        "scenario",
        choices=[
            "cancel_exclude_undo",
            "cancel_exclude_undo_after_schema",
            "week",
            "recovery_query",
            "browser",
        ],
    )
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "evaluation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another evaluator owns this ledger.") from None
        try:
            asyncio.run(main(parser.parse_args().scenario))
        except KeyboardInterrupt:
            pass

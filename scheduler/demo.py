"""Explicit fixed-script offline demo. This fixture is NOT a language model or parser.

Real application uses CompatibleModel. This separate entry point injects a scripted
Model only; the exact same Agent, tools, confirmation endpoints and SQLite execute.
"""

import argparse
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import uvicorn

from scheduler.app import create_app
from scheduler.config import Settings
from scheduler.provider import ModelError

DEMO_PROMPTS = [
    "明天和老王开会",
    "下午3点，一小时，地点会议室A",
    "我明天有什么安排？",
    "把和老王的会地点改成会议室B",
    "明天下午3点写周报一小时",
    "后天下午3点和老王开会一小时",
    "取消和老王的会",
]


def tool(name, **arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "demo_" + name,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


class ReplayModel:
    """Exact canned prompts and protocol responses; no heuristic intent classification."""

    async def complete(self, messages, tools):
        user_index = max(i for i, m in enumerate(messages) if m["role"] == "user")
        text = messages[user_index]["content"]
        observations = [
            json.loads(m["content"]) for m in messages[user_index + 1 :] if m["role"] == "tool"
        ]
        tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        event = {
            "title": "和老王开会",
            "start": tomorrow.isoformat(),
            "end": (tomorrow + timedelta(hours=1)).isoformat(),
            "location": "会议室A",
        }
        if text == DEMO_PROMPTS[0]:
            return tool("ask_user", fields=["start", "duration"])
        if text == DEMO_PROMPTS[1]:
            return tool("propose_create", **event)
        if text == DEMO_PROMPTS[2]:
            if not observations:
                return tool(
                    "search_events",
                    start=tomorrow.replace(hour=0).isoformat(),
                    end=(tomorrow + timedelta(days=1)).replace(hour=0).isoformat(),
                )
            return {"role": "assistant", "content": None}
        if text in (DEMO_PROMPTS[3], DEMO_PROMPTS[6]):
            if not observations:
                return tool("search_events", query="老王")
            candidates = observations[-1].get("events", [])
            if len(candidates) != 1:
                return tool("ask_user", fields=["selection"])
            return (
                tool("propose_update", event_id=candidates[0]["id"], patch={"location": "会议室B"})
                if text == DEMO_PROMPTS[3]
                else tool("propose_delete", event_id=candidates[0]["id"])
            )
        if text == DEMO_PROMPTS[4]:
            if not observations:
                return tool("propose_create", **{**event, "title": "写周报", "location": ""})
            if len(observations) == 1:
                return tool(
                    "find_slots",
                    start=event["start"],
                    end=tomorrow.replace(hour=19).isoformat(),
                    duration_minutes=60,
                )
            return tool(
                "ask_user",
                fields=["alternative"],
            )
        if text == DEMO_PROMPTS[5]:
            return tool(
                "propose_create",
                **{
                    **event,
                    "start": (tomorrow + timedelta(days=1)).isoformat(),
                    "end": (tomorrow + timedelta(days=1, hours=1)).isoformat(),
                },
            )
        if (
            text == "请继续处理我选择的这条日程"
            and "用户通过候选卡片明确选择了 event_id=" in messages[0]["content"]
        ):
            selected = (
                messages[0]["content"]
                .split("用户通过候选卡片明确选择了 event_id=")[-1]
                .splitlines()[0]
                .strip()
            )
            return tool("propose_delete", event_id=selected)
        raise ModelError(
            "当前是固定脚本演示，不会理解任意输入。请使用“演示下一步”，或配置真实模型后以普通模式启动。"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="固定脚本离线演示，无真实 LLM 调用")
    parser.add_argument(
        "--database", default="data/demo.sqlite3", help="独立演示数据库；重复演示请指定新的文件名"
    )
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    settings = Settings(database=args.database)
    uvicorn.run(
        create_app(settings, ReplayModel(), demo=True),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )

"""Observed, budgeted local web acceptance entry point; synthetic database required.

Normal user startup remains uvicorn scheduler.app:create_app --factory.
This wrapper injects only the measured HTTP transport, never model decisions.
"""

import asyncio
from pathlib import Path

import httpx
import uvicorn

from scheduler.app import create_app
from scheduler.config import Settings
from scheduler.provider import CompatibleModel
from scripts.live_support import ProbeTransport


async def main():
    settings = Settings.load()
    if (
        settings.model != "deepseek/deepseek-v4.1-flash"
        or settings.endpoint != "https://openrouter.ai/api/v1/chat/completions"
    ):
        raise SystemExit("Use the explicit DeepSeek/OpenRouter environment overrides.")
    if Path(settings.database).resolve() != Path("data/sprint-20261017/browser.sqlite3").resolve():
        raise SystemExit("Use the isolated browser acceptance database.")
    transport = ProbeTransport(
        "data/sprint-20261017/report.json",
        settings.endpoint,
        secret=settings.api_key.get_secret_value(),
    )
    transport.scenario = "browser_acceptance"
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False
    ) as client:
        app = create_app(settings, CompatibleModel(settings, client))
        await uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=8002, log_level="warning")
        ).serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Intentional local-server shutdown, not a model or tool failure.

"""Offline safety tests for the opt-in synthetic evaluation harness."""

import asyncio
import json

import httpx
import pytest

from scripts.live_support import ProbeTransport
from scheduler.provider import ModelError


def test_probe_budget_is_durable_and_excludes_secrets(tmp_path):
    async def run():
        log = tmp_path / "report.json"
        requests = []

        def handle(req):
            requests.append(req)
            return httpx.Response(
                200,
                json={
                    "id": "synthetic-id",
                    "model": "fixture",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "hello",
                                "reasoning": "private-thought",
                            }
                        }
                    ],
                    "usage": {"cost": 0.001},
                },
            )

        for _ in range(2):
            async with httpx.AsyncClient(
                transport=ProbeTransport(
                    log,
                    "https://openrouter.ai/api/v1/chat/completions",
                    limit=2,
                    secret="secret-value",
                    inner=httpx.MockTransport(handle),
                )
            ) as client:
                await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": "Bearer secret-value"},
                    json={"model": "fixture", "messages": []},
                )
        async with httpx.AsyncClient(
            transport=ProbeTransport(
                log,
                "https://openrouter.ai/api/v1/chat/completions",
                limit=2,
                secret="secret-value",
                inner=httpx.MockTransport(handle),
            )
        ) as client:
            with pytest.raises(ModelError, match="预算"):
                await client.post("https://openrouter.ai/api/v1/chat/completions", json={})
        assert len(requests) == 2
        text = log.read_text()
        assert "secret-value" not in text and "private-thought" not in text
        assert all(json.loads(req.content)["max_tokens"] == 2048 for req in requests)

    asyncio.run(run())


def test_probe_403_blocks_further_requests(tmp_path):
    async def run():
        requests = []

        def handle(req):
            requests.append(req)
            return httpx.Response(
                403,
                json={
                    "error": {
                        "message": "rejected secret-value",
                        "metadata": {"provider_name": "fixture"},
                    }
                },
                headers={"x-request-id": "request-fixture"},
            )

        log = tmp_path / "403.json"
        async with httpx.AsyncClient(
            transport=ProbeTransport(
                log,
                "https://openrouter.ai/api/v1/chat/completions",
                secret="secret-value",
                inner=httpx.MockTransport(handle),
            )
        ) as client:
            assert (
                await client.post("https://openrouter.ai/api/v1/chat/completions", json={})
            ).status_code == 403
            with pytest.raises(ModelError):
                await client.post("https://openrouter.ai/api/v1/chat/completions", json={})
        assert len(requests) == 1
        row = json.loads(log.read_text())["requests"][0]
        assert row["error"] == "rejected [REDACTED]"
        assert row["request_id"] == "request-fixture"
        assert row["provider"] == "fixture"

    asyncio.run(run())

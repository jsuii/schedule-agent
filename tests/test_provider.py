import asyncio
import json

import httpx
import pytest

from scheduler.config import Settings
from scheduler.provider import CompatibleModel, ModelError


def test_config_file_env_priority_and_secret_repr(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            dict(endpoint="http://localhost:8001/v1", model="local", api_key="${LLM_API_KEY}")
        )
    )
    s = Settings.load(
        p, env={"LLM_MODEL": "chosen", "LLM_API_KEY": "secret-value"}, dotenv_path=None
    )
    assert s.model == "chosen"
    assert s.endpoint == "http://localhost:8001/v1/chat/completions"
    assert s.api_key.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(s)


def test_provider_payload_and_no_secret_error():
    async def run():
        def handle(req):
            body = json.loads(req.content)
            assert body["tools"][0]["type"] == "function"
            assert body["model"] == "local"
            assert req.headers["authorization"] == "Bearer secret"
            return httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "你好"}}]}
            )

        s = Settings(endpoint="http://localhost:8001/v1", model="local", api_key="secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await CompatibleModel(s, client).complete(
                [{"role": "user", "content": "你好"}], [{"type": "function"}]
            )
        assert result["content"] == "你好"

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,body",
    [
        (401, "secret"),
        (400, "unsupported tools secret"),
        (429, "secret"),
        (500, "secret"),
        (200, {}),
    ],
)
def test_provider_errors_are_useful_and_redacted(status, body):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body))
        ) as client:
            with pytest.raises(ModelError) as err:
                await CompatibleModel(
                    Settings(endpoint="http://localhost:8001", model="x", api_key="secret"), client
                ).complete([], [])
            assert "secret" not in str(err.value)

    asyncio.run(run())


def test_timeout_and_malformed_tool_envelope():
    async def run():
        def timeout(req):
            raise httpx.ReadTimeout("secret")

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with pytest.raises(ModelError, match="超时"):
                await CompatibleModel(Settings(model="x"), client).complete([], [])
        body = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "x",
                                "type": "function",
                                "function": {"name": "bad", "arguments": {}},
                            }
                        ],
                    }
                }
            ]
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))
        ) as client:
            with pytest.raises(ModelError):
                await CompatibleModel(Settings(model="x"), client).complete([], [])

    asyncio.run(run())


def test_missing_model_configuration():
    with pytest.raises(ModelError, match="配置"):
        asyncio.run(CompatibleModel(Settings()).complete([], []))

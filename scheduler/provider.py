"""Replaceable model boundary. Only OpenAI-compatible Chat Completions tools are assumed."""

from typing import Protocol

import httpx

from scheduler.config import Settings


class ModelError(RuntimeError):
    pass


class Model(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        """Return assistant message with optional function tool_calls; never execute tools here."""
        ...


class CompatibleModel:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings, self.client = settings, client

    async def complete(self, messages, tools):
        if not self.settings.model.strip():
            raise ModelError("尚未配置 LLM_MODEL。请配置 endpoint、model 和 API key 后重试。")
        if self.client is None:
            async with httpx.AsyncClient(
                timeout=self.settings.timeout_seconds, follow_redirects=False
            ) as client:
                return await self._request(client, messages, tools)
        return await self._request(self.client, messages, tools)

    async def _request(self, client, messages, tools):
        headers = {"Content-Type": "application/json"}
        key = self.settings.api_key.get_secret_value()
        if key:
            headers["Authorization"] = "Bearer " + key
        try:
            response = await client.post(
                self.settings.endpoint,
                headers=headers,
                json={
                    "model": self.settings.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "stream": False,
                },
                timeout=self.settings.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise ModelError("模型请求超时，请稍后重试；日程没有因本次模型调用而改变。") from None
        except httpx.HTTPError:
            raise ModelError("无法连接模型端点，请检查 endpoint 和网络。") from None
        if response.status_code != 200:
            messages_by_code = {
                401: "模型认证失败，请检查 API key。",
                403: "模型权限不足。",
                400: "模型拒绝请求，请确认支持 Chat Completions tools 和 tool_choice。",
                404: "模型或 endpoint 不存在。",
                429: "模型限流，请稍后重试。",
            }
            raise ModelError(messages_by_code.get(response.status_code, "模型服务暂时不可用。"))
        # Never include the response body or httpx exception: either may contain secrets.
        try:
            message = response.json()["choices"][0]["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError()
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 8:
                raise ValueError()
            ids = set()
            for call in calls:
                if (
                    call["type"] != "function"
                    or not isinstance(call["id"], str)
                    or call["id"] in ids
                    or not isinstance(call["function"]["name"], str)
                    or not isinstance(call["function"]["arguments"], str)
                    or len(call["function"]["arguments"]) > 16000
                ):
                    raise ValueError()
                ids.add(call["id"])
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("模型返回了无效的 tool-calling 响应，请检查模型兼容性。") from None
        return {
            "role": "assistant",
            "content": message.get("content"),
            **({"tool_calls": calls} if calls else {}),
        }

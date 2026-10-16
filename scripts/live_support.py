"""Opt-in evaluation instrumentation; synthetic fixtures only, never used by the app."""

import json
import re
import time
from pathlib import Path

import httpx

from scheduler.provider import ModelError


class ProbeTransport(httpx.AsyncBaseTransport):
    def __init__(self, path, endpoint, *, limit=24, cost_limit=0.05, secret="", inner=None):
        self.path, self.endpoint = Path(path), endpoint
        self.limit, self.cost_limit, self.secret = limit, cost_limit, secret
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)
        self.scenario = "unspecified"
        if self.path.exists():
            self.report = json.loads(self.path.read_text())
        else:
            self.report = {"requests": [], "scenarios": []}
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.report, ensure_ascii=False, indent=2)
        if self.secret:
            text = text.replace(self.secret, "[REDACTED]")
        text = re.sub(r"sk-[A-Za-z0-9_-]{10,}", "[REDACTED]", text)
        self.path.write_text(text)

    async def handle_async_request(self, request):
        rows = self.report["requests"]
        cost = sum((r.get("usage") or {}).get("cost", 0) or 0 for r in rows)
        if len(rows) >= self.limit or cost >= self.cost_limit or self.report.get("blocked"):
            raise ModelError("真实评估预算已用完或服务已拒绝请求；不会继续发送。")
        if str(request.url) != self.endpoint:
            raise ModelError("拒绝非授权评估端点。")
        body = json.loads(request.content)
        row = {"number": len(rows) + 1, "scenario": self.scenario, "model": body.get("model")}
        rows.append(row)
        self.save()  # Count before sending, including timeouts and interrupted requests.
        body["max_tokens"] = 2048
        bounded = httpx.Request(
            request.method,
            request.url,
            headers={k: v for k, v in request.headers.items() if k.lower() != "content-length"},
            json=body,
            extensions=request.extensions,
        )
        start = time.monotonic()
        try:
            response = await self.inner.handle_async_request(bounded)
            await response.aread()
            try:
                data = response.json()
            except ValueError:
                data = {}
            error = data.get("error") or {}
            row.update(
                status_code=response.status_code,
                request_id=response.headers.get("x-request-id") or data.get("id"),
                provider=(error.get("metadata") or {}).get("provider_name") or data.get("provider"),
            )
            if response.status_code != 200:
                row["error"] = str(error.get("message", "Unrecognized error"))[:500]
                self.report["blocked"] = True  # No retry or provider/model sweep.
            else:
                row["usage"] = data.get("usage")
                row["response_model"] = data.get("model")
                message = (data.get("choices") or [{}])[0].get("message") or {}
                row["tools"] = message.get("tool_calls") or []
                row["answer"] = message.get("content")
                # Needed to reproduce a failed tool round-trip; never store reasoning.
                row["observations_sent"] = [
                    m for m in body.get("messages", [])[-8:] if m.get("role") == "tool"
                ]
            return response
        except Exception as exc:
            row["transport_error"] = type(exc).__name__
            raise
        finally:
            row["seconds"] = round(time.monotonic() - start, 2)
            self.save()

    async def aclose(self):
        await self.inner.aclose()

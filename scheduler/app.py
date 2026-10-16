"""Local single-user web application. Bind to loopback; not an internet service."""

import asyncio
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from scheduler.agent import Agent
from scheduler.calendar import CalendarError
from scheduler.config import Settings
from scheduler.provider import CompatibleModel
from scheduler.storage import Store


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Chat(Body):
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("输入不能为空")
        return value.strip()


class Decision(Body):
    approve: bool


class Selection(Body):
    event_id: str = Field(min_length=1, max_length=64)


def create_app(settings=None, model=None, *, demo=False):
    settings = settings or Settings.load()
    store = Store(settings.database, settings.timezone)
    model = model or CompatibleModel(settings)
    app = FastAPI(title="留白 · 日程 Agent", docs_url=None, redoc_url=None)
    app.state.settings, app.state.store, app.state.model = settings, store, model
    app.state.agent = Agent(store, model, settings)
    locks = defaultdict(asyncio.Lock)
    static = Path(__file__).parent / "static"
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def session_and_security(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                actual = urlsplit(origin)
                if actual.scheme not in ("http", "https") or actual.netloc != request.headers.get(
                    "host"
                ):
                    return JSONResponse({"message": "拒绝跨站请求"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"message": "拒绝跨站请求"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"message": "请使用 JSON 请求"}, status_code=415)
            try:
                if int(request.headers.get("content-length", "0")) > 32000:
                    return JSONResponse({"message": "请求体过大"}, status_code=413)
            except ValueError:
                return JSONResponse({"message": "无效的请求长度"}, status_code=400)
        sid = request.cookies.get("schedule_session")
        fresh = False
        try:
            store.session(sid)
        except CalendarError:
            sid, fresh = store.new_session(), True
        request.state.sid = sid
        response = await call_next(request)
        if fresh:
            response.set_cookie(
                "schedule_session",
                sid,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=30 * 86400,
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(CalendarError)
    async def business_error(request, exc):
        return JSONResponse(
            {
                "status": "error",
                "error": exc.code,
                "message": exc.details.get("hint", "请求无效，请刷新后重试。"),
            },
            status_code=404 if exc.code.endswith("not_found") else 409,
        )

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "timezone": settings.timezone, "demo": demo}

    @app.get("/api/state")
    async def state(request: Request):
        sid = request.state.sid
        session = store.session(sid)
        # User-visible conversation text only. Raw tool observations stay server-side.
        history = [
            {"role": m["role"], "content": m["content"]}
            for turn in session["turns"]
            for m in turn
            if m["role"] in ("user", "assistant") and m.get("content") and not m.get("tool_calls")
        ]
        return {
            "events": store.calendar.search(limit=501)[:500],
            "history": history,
            "events_truncated": len(store.calendar.search(limit=501)) > 500,
            "proposal": store.pending(sid),
            "candidates": session["state"].get("candidates", []),
            "timezone": settings.timezone,
            "model_configured": bool(settings.model.strip()),
            "demo": demo,
        }

    @app.post("/api/chat")
    async def chat(body: Chat, request: Request):
        sid = request.state.sid
        # No global lock around model I/O: distinct conversations may progress independently.
        lock = locks[sid]
        if lock.locked():
            raise HTTPException(409, "本会话正在处理另一条消息，请稍候。")
        async with lock:
            cached = store.claim_request(sid, body.request_id)
            if cached:
                return cached
            result = await app.state.agent.run(sid, body.text)
            store.finish_request(sid, body.request_id, result)
            return result

    @app.post("/api/proposals/{proposal_id}")
    async def confirm(proposal_id: str, body: Decision, request: Request):
        sid = request.state.sid
        if locks[sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候再确认。")
        return store.confirm(sid, proposal_id, body.approve)

    @app.post("/api/select")
    async def select(body: Selection, request: Request):
        sid = request.state.sid
        if locks[sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候再选择。")
        store.select(sid, body.event_id)
        return {"status": "selected", "event_id": body.event_id}

    @app.post("/api/session")
    async def new_session():
        sid = store.new_session()
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            "schedule_session", sid, httponly=True, samesite="strict", max_age=30 * 86400
        )
        return response

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app

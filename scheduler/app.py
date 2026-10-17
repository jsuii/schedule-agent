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


class BatchSelection(Body):
    selected_ids: list[str] = Field(min_length=1, max_length=1000)


class TrashReference(Body):
    id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    deletion_batch_id: str = Field(min_length=1, max_length=64)


class RestoreSelection(Body):
    selected: list[TrashReference] = Field(min_length=1, max_length=1000)


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
                limit = (
                    200000
                    if request.url.path.startswith(("/api/cancellations/", "/api/trash/"))
                    else 32000
                )
                if int(request.headers.get("content-length", "0")) > limit:
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
        return {
            "status": "ok",
            "timezone": settings.timezone,
            "demo": demo,
            "model": settings.model if not demo else None,
        }

    def undo_available(sid):
        last = store.last_deletion(sid)
        if not last:
            return None
        from scheduler.calendar import RETENTION_SECONDS
        import time

        eligible = [
            e
            for target in last["event_ids"]
            if (e := store.calendar.trash_get(target))
            and e["deletion_batch_id"] == last["operation_id"]
            and store.calendar.timestamp(e["deleted_at"]) + RETENTION_SECONDS > time.time()
        ]
        return {"operation_id": last["operation_id"], "count": len(eligible)} if eligible else None

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
        from scheduler.tasks import clarification_view

        pending = store.pending(sid)
        clarification = (
            clarification_view(session["state"].get("task"))
            if not pending and not session["state"].get("conflict")
            else None
        )
        from scheduler.flow import contexts

        review = session["state"].get("slot_review")
        import time

        if pending or not review or review["expires"] <= time.time():
            review = None
        return {
            "contexts": contexts(store, sid),
            "active_context_id": session["state"].get("context_id"),
            "slot_review": review,
            "clarification": clarification,
            "events": store.calendar.search(limit=501)[:500],
            "history": history,
            "events_truncated": len(store.calendar.search(limit=501)) > 500,
            "proposal": pending,
            "undo": undo_available(sid),
            "conflict": session["state"].get("conflict"),
            "candidates": session["state"].get("candidates", []),
            "timezone": settings.timezone,
            "model_configured": bool(settings.model.strip()),
            "model": settings.model if not demo else None,
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
            result["request_id"] = body.request_id
            store.finish_request(sid, body.request_id, result)
            return result

    @app.post("/api/contexts/{context_id}/{action}")
    async def context_action(context_id: str, action: str, request: Request):
        if locks[request.state.sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候")
        if action not in ("resume", "cancel"):
            raise HTTPException(404, "操作不存在")
        from scheduler.flow import control, phase
        from scheduler.tools import Tools, Context

        result = control(
            Tools(store, request.state.sid), Context(action=action, task_id=context_id)
        )
        phase(store, request.state.sid, "awaiting_clarification")
        return {
            "context": result.get("context"),
            "message": result.get("question", "已恢复该任务，请核对当前内容后继续。"),
        }

    @app.post("/api/slots/{review_id}/{option_id}")
    async def slot_choice(review_id: str, option_id: str, request: Request):
        if locks[request.state.sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候")
        from scheduler.flow import choose_slot

        return {"proposal": choose_slot(store, request.state.sid, review_id, option_id)}

    @app.post("/api/proposals/{proposal_id}")
    async def confirm(proposal_id: str, body: Decision, request: Request):
        sid = request.state.sid
        if locks[sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候再确认。")
        return store.confirm(sid, proposal_id, body.approve)

    @app.post("/api/cancellations/{proposal_id}/selection")
    async def revise(proposal_id: str, body: BatchSelection, request: Request):
        if locks[request.state.sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候")
        from scheduler.trash import revise_cancel

        return {"proposal": revise_cancel(store, request.state.sid, proposal_id, body.selected_ids)}

    @app.get("/api/trash")
    async def trash_view():
        rows = store.calendar.trash(limit=1001)
        return {"events": rows[:1000], "truncated": len(rows) > 1000, "retention_days": 7}

    @app.post("/api/trash/restore")
    async def restore_selected(body: RestoreSelection, request: Request):
        if locks[request.state.sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候")
        from scheduler.trash import propose_restore

        expected = [r.model_dump() for r in body.selected]
        return {
            "proposal": propose_restore(
                store, request.state.sid, event_ids=[e["id"] for e in expected], expected=expected
            )
        }

    @app.post("/api/trash/restore/{proposal_id}/options/{option_id}")
    async def restore_option(proposal_id: str, option_id: str, request: Request):
        if locks[request.state.sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候")
        from scheduler.trash import choose_restore

        return {"proposal": choose_restore(store, request.state.sid, proposal_id, option_id)}

    @app.post("/api/trash/batches/{batch_id}")
    async def restore_batch(batch_id: str, request: Request):
        if locks[request.state.sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候")
        from scheduler.trash import propose_restore

        rows = store.calendar.trash(batch_id=batch_id, limit=1001)
        if len(rows) > 1000:
            raise CalendarError("batch_too_large", hint="恢复批次超过1000项，请逐组选择")
        return {
            "proposal": propose_restore(
                store, request.state.sid, event_ids=[e["id"] for e in rows], expected=rows
            )
        }

    @app.post("/api/undo/{operation_id}")
    async def undo(operation_id: str, request: Request):
        if locks[request.state.sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候")
        from scheduler.trash import undo

        return undo(store, request.state.sid, operation_id)

    @app.post("/api/plans/{plan_id}")
    async def choose_plan(plan_id: str, request: Request):
        sid = request.state.sid
        if locks[sid].locked():
            raise HTTPException(409, "助手正在处理，请稍候再选择方案。")
        return {"status": "proposal", "proposal": store.choose_plan(sid, plan_id)}

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

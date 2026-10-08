"""认证后的 SSE 聊天入口。"""

import asyncio
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from agent.guards.authorization import Principal, authenticate
from agent.observability.metrics import HTTP_DURATION, HTTP_REQUESTS, SSE_ACTIVE
from agent.observability.tracing import ActionError
from api.schemas import ChatRequest, Event
from api.sse import encode

router = APIRouter()


@router.post("/chat")
async def chat(
    body: ChatRequest, request: Request, principal: Annotated[Principal, Depends(authenticate)]
) -> StreamingResponse:
    workflow = request.app.state.workflow
    settings = request.app.state.settings
    trace_id = "tr_" + uuid.uuid4().hex
    if body.action is None:
        try:
            request.app.state.store.last_order(body.conversation_id, principal)
        except ActionError as exc:
            raise HTTPException(403, detail={"code": exc.code, "message": "会话不可访问"}) from None

    async def events():
        started = time.perf_counter()
        status = "ok"
        SSE_ACTIVE.inc()
        try:
            async with asyncio.timeout(settings.request_deadline_seconds):
                async for event in workflow.stream(body, principal, trace_id):
                    if await request.is_disconnected():
                        status = "cancelled"
                        break
                    yield encode(event)
        except TimeoutError:
            status = "timeout"
            yield encode(Event(event="error", data={"code": "UPSTREAM_TIMEOUT", "message": "请求处理超时"}))
            yield encode(Event(event="done", data={"trace_id": trace_id, "citations": []}))
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            SSE_ACTIVE.dec()
            HTTP_DURATION.labels("chat").observe(time.perf_counter() - started)
            HTTP_REQUESTS.labels("chat", status).inc()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )

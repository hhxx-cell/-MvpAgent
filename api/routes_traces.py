"""演示环境的受保护 Trace 导出。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from agent.guards.authorization import Principal, authenticate, require_scope

router = APIRouter()


@router.get("/internal/traces/{trace_id}")
async def get_trace(trace_id: str, request: Request, principal: Annotated[Principal, Depends(authenticate)]) -> dict:
    require_scope(principal, "traces:read")
    trace = request.app.state.store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(404, detail={"code": "FORBIDDEN_RESOURCE", "message": "未找到或无权访问"})
    return trace

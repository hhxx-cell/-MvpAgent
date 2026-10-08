"""受令牌保护的 Prometheus 指标。"""

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter()


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    supplied = request.headers.get("X-Metrics-Token", "")
    expected = request.app.state.settings.metrics_token
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(403, detail={"code": "FORBIDDEN_RESOURCE", "message": "无权访问"})
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

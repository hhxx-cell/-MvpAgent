"""业务工具客户端、权限注入和受截止时间约束的重试。"""

import asyncio
import hashlib
import random
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from agent.config import Settings
from agent.guards.authorization import Principal
from agent.observability.metrics import TOOL_CALLS, TOOL_FAILURES, TOOL_RETRIES


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ToolClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, scenario: str | None = None):
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=settings.mock_server_url,
            timeout=httpx.Timeout(connect=1.0, read=2.0, write=2.0, pool=1.0),
        )
        # Only tests or server configuration may inject a failure scenario.
        self.scenario = scenario if settings.app_env in {"development", "test"} else None
        self.random = random.Random(settings.mock_random_seed)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self, principal: Principal) -> dict[str, str]:
        headers = {
            "X-Internal-Service-Key": self.settings.mock_internal_key,
            "X-Principal-ID": principal.principal_id,
            "X-Tenant-ID": principal.tenant_id,
        }
        if self.scenario and self.settings.mock_scenario_control_enabled:
            headers["X-Mock-Scenario"] = self.scenario
        return headers

    async def _request(
        self,
        tool: str,
        method: str,
        path: str,
        principal: Principal,
        *,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        headers = self._headers(principal)
        headers.update(extra_headers or {})
        end = deadline or time.monotonic() + self.settings.request_deadline_seconds
        reason = "UPSTREAM_TIMEOUT"
        for attempt in range(1, self.settings.tool_max_attempts + 1):
            if time.monotonic() >= end:
                break
            try:
                timeout = min(2.5, max(0.1, end - time.monotonic()))
                response = await asyncio.wait_for(
                    self.client.request(method, path, headers=headers, json=json_body, timeout=timeout),
                    timeout=timeout,
                )
                if response.status_code == 200 or response.status_code == 201:
                    TOOL_CALLS.labels(tool, "success").inc()
                    result = response.json()
                    if not isinstance(result, dict):
                        raise ToolError("INTERNAL_ERROR", "上游响应格式错误")
                    return result
                if response.status_code in {401, 403, 404}:
                    TOOL_FAILURES.labels(tool, "authorization_or_missing").inc()
                    raise ToolError("FORBIDDEN_RESOURCE", "未找到或无权访问")
                if response.status_code == 429:
                    reason = "UPSTREAM_RATE_LIMIT"
                elif response.status_code in {500, 502, 503, 504}:
                    reason = "UPSTREAM_ERROR"
                else:
                    TOOL_FAILURES.labels(tool, "permanent").inc()
                    raise ToolError("INTERNAL_ERROR", "业务服务拒绝请求")
                retry_after = response.headers.get("Retry-After") if response.status_code == 429 else None
            except (TimeoutError, httpx.TimeoutException, httpx.NetworkError):
                reason = "UPSTREAM_TIMEOUT"
                retry_after = None
            if attempt >= self.settings.tool_max_attempts:
                break
            TOOL_RETRIES.labels(tool, reason).inc()
            try:
                wait = (
                    max(0.0, float(retry_after))
                    if retry_after
                    else min(0.5 * (2 ** (attempt - 1)) + self.random.uniform(0, 0.1), 2.0)
                )
            except ValueError:
                wait = 0.5
            if time.monotonic() + wait >= end:
                break
            await asyncio.sleep(wait)
        TOOL_CALLS.labels(tool, "failed").inc()
        TOOL_FAILURES.labels(tool, reason).inc()
        messages = {
            "UPSTREAM_RATE_LIMIT": "订单服务请求过于频繁，请稍后重试",
            "UPSTREAM_TIMEOUT": "订单服务暂时未响应",
            "UPSTREAM_ERROR": "订单服务暂时不可用",
        }
        raise ToolError(reason, messages.get(reason, "业务服务暂时不可用"))

    async def query_order(self, order_id: str, principal: Principal, deadline: float | None = None) -> dict[str, Any]:
        if not principal.has("orders:read") or not re.fullmatch(r"O\d{5,}", order_id):
            raise ToolError("FORBIDDEN_RESOURCE", "未找到或无权访问")
        return await self._request("query_order", "GET", f"/orders/{order_id}", principal, deadline=deadline)

    async def query_logistics(
        self,
        order_id: str,
        principal: Principal,
        deadline: float | None = None,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        order = await self.query_order(order_id, principal, deadline)
        if on_progress:
            on_progress("query_order", "success")
            on_progress("query_logistics", "started")
        logistics = await self._request(
            "query_logistics", "GET", f"/orders/{order_id}/logistics", principal, deadline=deadline
        )
        return order, logistics

    async def create_ticket(
        self,
        order_id: str,
        issue_type: str,
        summary: str,
        action_id: str,
        principal: Principal,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        if not principal.has("tickets:write") or not re.fullmatch(r"act_[0-9a-f]{32}", action_id):
            raise ToolError("FORBIDDEN_RESOURCE", "无权创建工单")
        if (
            not re.fullmatch(r"O\d{5,}", order_id)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", issue_type)
            or not 1 <= len(summary) <= 500
        ):
            raise ToolError("MISSING_ARGUMENT", "工单参数无效")
        # Idempotency key is deterministic and hidden from the model/client.
        key = hashlib.sha256(action_id.encode()).hexdigest()
        return await self._request(
            "create_ticket",
            "POST",
            "/tickets",
            principal,
            json_body={"order_id": order_id, "issue_type": issue_type, "summary": summary, "action_id": action_id},
            extra_headers={"Idempotency-Key": key},
            deadline=deadline,
        )

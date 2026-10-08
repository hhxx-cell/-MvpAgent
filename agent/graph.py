"""MVP 的显式 LangGraph 路由、取证、工具和答案流程。"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.graph import END, START, StateGraph

from agent.config import Settings
from agent.guards.authorization import Principal, owned_order_exists
from agent.guards.pii import redact
from agent.model_gateway import ModelGateway
from agent.observability.metrics import (
    AGENT_ROUTE,
    LLM_TOKENS,
    RAG_DURATION,
    RAG_NO_EVIDENCE,
    SQL_CORRECTION,
    SQL_DURATION,
    SQL_REJECTED,
    SQL_TEMPLATE_FALLBACK,
    TICKET_CREATED,
    TICKET_IDEMPOTENCY,
)
from agent.observability.tracing import ActionError, TraceStore
from agent.retriever.service import Retriever
from agent.router import Route
from agent.sql.correction import run_sql_with_corrections
from agent.sql.executor import SQLExecutionError
from agent.sql.generator import SQLCandidate, UnsupportedSQLQuestion, generate_sql, linked_tables
from agent.sql.schema_catalog import SchemaCatalog
from agent.sql.validator import SQLValidationError
from agent.state import AgentState
from agent.tools.registry import ToolClient, ToolError
from api.schemas import ChatRequest, Event


def span(name: str, started: float, status: str = "ok", **extra: Any) -> dict[str, Any]:
    duration = time.perf_counter() - started
    ended_at = datetime.now(UTC)
    return {
        "name": name,
        "started_at": (ended_at - timedelta(seconds=duration)).isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_ms": round(duration * 1000, 2),
        "status": status,
        **extra,
    }


class AgentWorkflow:
    def __init__(
        self,
        settings: Settings,
        store: TraceStore,
        retriever: Retriever,
        catalog: SchemaCatalog,
        tool_client: ToolClient,
        gateway: ModelGateway,
    ):
        self.settings = settings
        self.store = store
        self.retriever = retriever
        self.catalog = catalog
        self.tools = tool_client
        self.gateway = gateway
        graph = StateGraph(AgentState)
        graph.add_node("route", self._route)
        graph.add_node("clarify", self._clarify)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("sql", self._sql)
        graph.add_node("tools", self._tools)
        graph.add_node("action", self._action)
        graph.add_node("compose", self._compose)
        graph.add_edge(START, "route")
        graph.add_conditional_edges(
            "route",
            self._route_next,
            {
                "action": "action",
                "clarify": "clarify",
                "retrieve": "retrieve",
            },
        )
        graph.add_edge("retrieve", "sql")
        graph.add_edge("sql", "tools")
        graph.add_edge("tools", "compose")
        graph.add_edge("action", "compose")
        graph.add_edge("clarify", "compose")
        graph.add_edge("compose", END)
        self.graph = graph.compile()

    async def _route(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        if state.get("action"):
            route = Route(("action",))
            tokens = 0
        else:
            last_order = self.store.last_order(state["conversation_id"], state["principal"])
            route, tokens = await self.gateway.classify(state["message"], last_order)
            LLM_TOKENS.labels(self.settings.llm_provider).inc(tokens)
        for intent in route.intents or ("clarify",):
            AGENT_ROUTE.labels(intent).inc()
        return {
            "route": route,
            "order_id": route.order_id,
            "llm_tokens": state.get("llm_tokens", 0) + tokens,
            "spans": [*state.get("spans", []), span("route", started, intents=list(route.intents))],
        }

    @staticmethod
    def _route_next(state: AgentState) -> str:
        if state.get("action"):
            return "action"
        if state["route"].clarification:
            return "clarify"
        return "retrieve"

    @staticmethod
    def _clarify(state: AgentState) -> dict[str, Any]:
        return {"final_answer": state["route"].clarification or "请补充必要信息。", "error_code": "MISSING_ARGUMENT"}

    def _retrieve(self, state: AgentState) -> dict[str, Any]:
        if "rag" not in state["route"].intents:
            return {}
        started = time.perf_counter()
        try:
            result = self.retriever.answer(state["message"], state["route"].policy_key or "")
            RAG_DURATION.observe(time.perf_counter() - started)
            if "RAG_NO_EVIDENCE" in result.warnings:
                RAG_NO_EVIDENCE.inc()
            return {
                "retrieval": result,
                "citations": result.citations,
                "warnings": [*state.get("warnings", []), *result.warnings],
                "spans": [
                    *state.get("spans", []),
                    span(
                        "retrieval",
                        started,
                        status="ok" if result.citations else "no_evidence",
                        citations=len(result.citations),
                    ),
                ],
            }
        except Exception:
            RAG_NO_EVIDENCE.inc()
            return {
                "error_code": "RAG_NO_EVIDENCE",
                "spans": [*state.get("spans", []), span("retrieval", started, "error")],
            }

    async def _sql(self, state: AgentState) -> dict[str, Any]:
        if "sql" not in state["route"].intents:
            return {}
        started = time.perf_counter()
        if not state["principal"].has("orders:read"):
            return {
                "error_code": "FORBIDDEN_RESOURCE",
                "spans": [*state.get("spans", []), span("sql_authorization", started, "rejected")],
            }
        try:
            candidate = generate_sql(state["message"], self.catalog, now=self.settings.analysis_now())
            schema = self.catalog.schema_summary(linked_tables(state["message"]))
            sql_text, tokens = await self.gateway.generate_sql(state["message"], schema, candidate.sql)
            LLM_TOKENS.labels(self.settings.llm_provider).inc(tokens)
            offered = SQLCandidate(sql_text, candidate.parameters, source=self.settings.llm_provider)
            run_args = (
                state["principal"].principal_id,
                state["principal"].tenant_id,
                self.catalog,
                self.settings.resolved_database_path(),
            )
            run_options = dict(
                max_corrections=self.settings.sql_max_corrections,
                deadline_seconds=min(10.0, self.settings.request_deadline_seconds),
                max_rows=self.settings.sql_max_rows,
            )
            template_fallback = False
            try:
                result = await asyncio.to_thread(run_sql_with_corrections, offered, *run_args, **run_options)
            except SQLValidationError as exc:
                if (
                    self.settings.llm_provider != "openai_compatible"
                    or exc.code == "FORBIDDEN_RESOURCE"
                    or offered.sql == candidate.sql
                ):
                    raise
                SQL_REJECTED.labels(
                    exc.code if exc.code in {"SQL_REJECTED", "SQL_PARSE_ERROR", "FORBIDDEN_RESOURCE"} else "other"
                ).inc()
                result = await asyncio.to_thread(run_sql_with_corrections, candidate, *run_args, **run_options)
                template_fallback = True
                SQL_TEMPLATE_FALLBACK.inc()
            SQL_DURATION.observe(time.perf_counter() - started)
            if result.corrections:
                SQL_CORRECTION.inc(result.corrections)
            return {
                "sql_rows": result.rows,
                "sql_template": result.query.sql,
                "sql_attempts": result.corrections + 1 + int(template_fallback),
                "llm_tokens": tokens,
                "spans": [
                    *state.get("spans", []),
                    span(
                        "schema_linking_sql_execution",
                        started,
                        rows=len(result.rows),
                        corrections=result.corrections,
                        template_fallback=template_fallback,
                        sql_template=redact(result.query.sql),
                    ),
                ],
            }
        except SQLValidationError as exc:
            SQL_REJECTED.labels(
                exc.code if exc.code in {"SQL_REJECTED", "SQL_PARSE_ERROR", "FORBIDDEN_RESOURCE"} else "other"
            ).inc()
            return {
                "error_code": "FORBIDDEN_RESOURCE" if exc.code == "FORBIDDEN_RESOURCE" else "SQL_REJECTED",
                "spans": [*state.get("spans", []), span("sql_validation", started, "rejected", error_code=exc.code)],
            }
        except (SQLExecutionError, UnsupportedSQLQuestion, ValueError):
            return {
                "error_code": "SQL_EXECUTION_FAILED",
                "spans": [*state.get("spans", []), span("sql_execution", started, "error")],
            }
        except Exception:
            return {
                "error_code": "SQL_EXECUTION_FAILED",
                "spans": [*state.get("spans", []), span("sql_execution", started, "error")],
            }

    async def _tools(self, state: AgentState) -> dict[str, Any]:
        intents = state["route"].intents
        if not any(intent in intents for intent in ("order", "logistics", "ticket")):
            return {}
        started = time.perf_counter()
        order_id = state.get("order_id")
        if not order_id:
            return {"error_code": "MISSING_ARGUMENT"}
        principal = state["principal"]
        tool_events: list[dict[str, str]] = []
        try:
            if "logistics" in intents:
                tool_events.append({"tool": "query_order", "status": "started"})
                order, logistics = await self.tools.query_logistics(
                    order_id,
                    principal,
                    on_progress=lambda tool, status: tool_events.append({"tool": tool, "status": status}),
                )
                tool_events.append({"tool": "query_logistics", "status": "success"})
                result: dict[str, Any] = {
                    "order": order.get("order", order),
                    "logistics": logistics.get("logistics", logistics),
                    "observed_at": logistics.get("observed_at"),
                }
            else:
                tool_events.append({"tool": "query_order", "status": "started"})
                order = await self.tools.query_order(order_id, principal)
                tool_events.append({"tool": "query_order", "status": "success"})
                result = {"order": order.get("order", order), "observed_at": order.get("observed_at")}
            self.store.remember_order(state["conversation_id"], principal, order_id)
            order_status = result["order"].get("status")
            logistics_status = result.get("logistics", {}).get("status")
            pending = None
            if "ticket" in intents or order_status == 5 or logistics_status == 3:
                issue = "refund_failure" if order_status == 5 else "logistics_exception"
                if "ticket" in intents and order_status != 5 and logistics_status != 3:
                    issue = "aftersales_request"
                payload = {"order_id": order_id, "issue_type": issue, "summary": f"订单 {order_id} 售后异常需人工处理"}
                pending = self.store.preview_action(state["conversation_id"], principal, payload)
                pending.update({"action": "create_ticket", "summary": f"为订单 {order_id} 创建售后工单"})
            return {
                "tool_result": result,
                "tool_events": tool_events,
                "pending_action": pending,
                "spans": [
                    *state.get("spans", []),
                    span(
                        "tool_call",
                        started,
                        tools=[item["tool"] for item in tool_events if item["status"] == "success"],
                    ),
                ],
            }
        except ToolError as exc:
            pending_tool = next(
                (item["tool"] for item in reversed(tool_events) if item["status"] == "started"),
                "query_order",
            )
            tool_events.append({"tool": pending_tool, "status": "failed"})
            pending = None
            if exc.code in {"UPSTREAM_RATE_LIMIT", "UPSTREAM_TIMEOUT", "UPSTREAM_ERROR"}:
                authorized = await asyncio.to_thread(
                    owned_order_exists, self.settings.resolved_database_path(), principal, order_id
                )
                if authorized:
                    pending = self.store.preview_action(
                        state["conversation_id"],
                        principal,
                        {
                            "order_id": order_id,
                            "issue_type": "service_unavailable",
                            "summary": f"订单 {order_id} 实时状态暂不可核实，申请人工处理",
                        },
                    )
                    pending.update({"action": "create_ticket", "summary": f"为订单 {order_id} 创建人工处理工单"})
            return {
                "error_code": exc.code,
                "tool_events": tool_events,
                "pending_action": pending,
                "spans": [*state.get("spans", []), span("tool_call", started, "error", error_code=exc.code)],
            }

    async def _action(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        action = state["action"] or {}
        principal = state["principal"]
        try:
            claim = self.store.claim_action(
                action.get("action_id", ""), state["conversation_id"], principal, action.get("decision", "")
            )
            if claim.status in {"replay", "cancelled"}:
                TICKET_IDEMPOTENCY.inc()
                return {
                    "tool_result": claim.result or {"status": claim.status},
                    "spans": [*state.get("spans", []), span("action_replay", started)],
                }
            if claim.status == "in_progress":
                return {
                    "tool_result": {"status": "processing", "message": "工单正在提交"},
                    "spans": [*state.get("spans", []), span("action_in_progress", started)],
                }
            payload = claim.payload or {}
            try:
                result = await self.tools.create_ticket(
                    payload["order_id"],
                    payload["issue_type"],
                    payload["summary"],
                    claim.action_id,
                    principal,
                )
                self.store.finish_action(claim.action_id, result)
                TICKET_CREATED.inc()
            except ToolError as exc:
                if exc.code in {"UPSTREAM_RATE_LIMIT", "UPSTREAM_TIMEOUT", "UPSTREAM_ERROR"}:
                    try:
                        result = self.store.enqueue_outbox(
                            claim.action_id,
                            {
                                **payload,
                                "principal_id": principal.principal_id,
                                "tenant_id": principal.tenant_id,
                            },
                            exc.code,
                        )
                    except Exception:
                        result = {"status": "failed", "message": "工单创建失败，请稍后重试"}
                        self.store.finish_action(claim.action_id, result, "failed")
                else:
                    result = {"status": "failed", "message": "工单创建失败，请稍后重试"}
                    self.store.finish_action(claim.action_id, result, "failed")
            return {
                "tool_result": result,
                "tool_events": [{"tool": "create_ticket", "status": result.get("status", "failed")}],
                "spans": [
                    *state.get("spans", []),
                    span("create_ticket", started, status=result.get("status", "failed")),
                ],
            }
        except ActionError as exc:
            return {
                "error_code": exc.code,
                "spans": [
                    *state.get("spans", []),
                    span("action_confirmation", started, "rejected", error_code=exc.code),
                ],
            }

    def _compose(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        if state.get("final_answer"):
            answer = state["final_answer"]
        elif state.get("action"):
            result = state.get("tool_result", {})
            status = result.get("status")
            if state.get("error_code") == "FORBIDDEN_RESOURCE":
                answer = "未找到或无权访问该操作。"
            elif status == "created":
                answer = f"工单已创建，工单号 {result.get('ticket_id', '')}。"
            elif status == "pending":
                answer = f"工单待提交，待处理编号 {result.get('pending_ticket_id', '')}。"
            elif status in {"cancelled", "processing"}:
                answer = result.get("message", "操作正在处理。")
            else:
                answer = "工单创建失败或确认无效。"
        else:
            parts: list[str] = []
            if state.get("retrieval"):
                parts.append(state["retrieval"].answer)
            if "sql" in state["route"].intents:
                parts.append(self._format_sql(state))
            if any(intent in state["route"].intents for intent in ("order", "logistics", "ticket")):
                parts.append(self._format_tool(state))
            answer = "\n".join(part for part in parts if part) or "暂时无法完成该请求。"
            if state.get("pending_action"):
                answer += " 如需创建售后工单，请确认上方操作预览。"
        return {"final_answer": answer, "spans": [*state.get("spans", []), span("answer_verification", started)]}

    def _format_sql(self, state: AgentState) -> str:
        if state.get("error_code") == "FORBIDDEN_RESOURCE":
            return "无权查询该数据。"
        if state.get("error_code") == "SQL_REJECTED":
            return "查询未通过安全校验。"
        if state.get("error_code") == "SQL_EXECUTION_FAILED":
            return "暂时无法完成数据查询，请稍后重试。"
        rows = state.get("sql_rows", [])
        if not rows:
            return "未查到符合条件的数据。"
        row = rows[0]
        if "total_cents" in row:
            amount = int(row["total_cents"] or 0) / 100
            return f"符合条件的消费金额为 {amount:.2f} 元（CNY），共 {row.get('order_count', 0)} 笔订单。"
        if "order_count" in row:
            return f"符合条件的订单共 {row['order_count']} 笔。"
        if "logistics_id" in row:
            order_status = self.catalog.status_codes["orders"].get(int(row["order_status"]), "未知")
            logistics_status = (
                self.catalog.status_codes["logistics"].get(int(row["logistics_status"]), "未知")
                if row["logistics_status"] is not None
                else "暂无物流"
            )
            return (
                f"订单 {row['order_id']} 的历史状态为{order_status}，金额为 "
                f"{int(row['amount_cents']) / 100:.2f} 元（CNY）；物流历史状态为"
                f"{logistics_status}，更新时间 {row['logistics_updated_at']}。"
            )
        if "amount_cents" in row:
            status = self.catalog.status_codes["orders"].get(int(row["status"]), "未知")
            return (
                f"订单 {row['order_id']} 的历史金额为 {int(row['amount_cents']) / 100:.2f} 元"
                f"（{row['currency']}），状态为{status}，快照时间 {row['updated_at']}。"
            )
        return "已查到数据，但当前问题需要更明确的统计口径。"

    @staticmethod
    def _format_tool(state: AgentState) -> str:
        code = state.get("error_code")
        if code == "FORBIDDEN_RESOURCE":
            return "未找到或无权访问该订单。"
        if code and code.startswith("UPSTREAM_"):
            return "当前无法核实实时订单或物流状态，请稍后重试；如需售后处理，可稍后申请工单。"
        result = state.get("tool_result", {})
        order = result.get("order", {})
        if not order:
            return "未查到订单信息。"
        text = (
            f"订单 {order.get('order_id', '')} 当前状态为{order.get('status_label', '未知')}，"
            f"金额 {int(order.get('amount_cents', 0)) / 100:.2f} 元（CNY），"
            f"查询时间 {result.get('observed_at', order.get('updated_at', '未知'))}。"
        )
        logistics = result.get("logistics")
        if logistics:
            text += (
                f"物流状态为{logistics.get('status_label', '未知')}，更新时间 {logistics.get('updated_at', '未知')}。"
            )
        return text

    async def stream(
        self, request: ChatRequest, principal: Principal, trace_id: str | None = None
    ) -> AsyncIterator[Event]:
        trace_id = trace_id or "tr_" + uuid.uuid4().hex
        stream_started = time.perf_counter()
        state: AgentState = {
            "conversation_id": request.conversation_id,
            "message": request.message,
            "action": request.action.model_dump() if request.action else None,
            "principal": principal,
            "trace_id": trace_id,
            "spans": [span("authentication", time.perf_counter())],
            "warnings": [],
            "citations": [],
            "llm_tokens": 0,
        }
        yield Event(event="step.started", data={"step": "route", "message": "正在识别请求"})
        try:
            async for update in self.graph.astream(state, stream_mode="updates"):
                for node, changes in update.items():
                    changes = changes or {}
                    state.update(changes)
                    if node == "route":
                        next_step = self._route_next(state)
                        yield Event(event="step.started", data={"step": next_step, "message": "正在处理请求"})
                    elif node in {"retrieve", "sql", "tools", "action", "clarify"}:
                        if node == "tools" or node == "action":
                            for item in changes.get("tool_events", []):
                                event_type = "tool.started" if item["status"] == "started" else "tool.completed"
                                yield Event(
                                    event=event_type,
                                    data={
                                        "tool": item["tool"],
                                        "status": item["status"],
                                        "summary": (
                                            "工具调用已完成" if item["status"] == "success" else "工具调用状态已更新"
                                        ),
                                    },
                                )
                        if changes.get("pending_action"):
                            yield Event(event="action.preview", data=changes["pending_action"])
                        if node == "retrieve" and changes.get("warnings"):
                            for warning in changes["warnings"]:
                                yield Event(event="warning", data={"code": warning, "message": "知识证据可能不足"})
                        upcoming = {
                            "retrieve": "sql",
                            "sql": "tools",
                            "tools": "compose",
                            "action": "compose",
                            "clarify": "compose",
                        }[node]
                        yield Event(event="step.started", data={"step": upcoming, "message": "正在处理请求"})
            answer = state.get("final_answer", "暂时无法完成该请求。")
            for start in range(0, len(answer), 50):
                yield Event(event="message.delta", data={"content": answer[start : start + 50]})
            yield Event(event="done", data={"trace_id": trace_id, "citations": state.get("citations", [])})
        except asyncio.CancelledError:
            state["error_code"] = "CANCELLED"
            raise
        except Exception:
            state["error_code"] = "INTERNAL_ERROR"
            yield Event(event="error", data={"code": "INTERNAL_ERROR", "message": "服务暂时不可用"})
            yield Event(event="done", data={"trace_id": trace_id, "citations": []})
        finally:
            try:
                state["spans"] = [*state.get("spans", []), span("response_stream", stream_started)]
                self.store.save_trace(
                    trace_id,
                    request.conversation_id,
                    principal,
                    {
                        "trace_id": trace_id,
                        "prompt_summary": redact(request.message[:500]),
                        "route": list(state.get("route", Route()).intents),
                        "spans": state.get("spans", []),
                        "sql_template": state.get("sql_template"),
                        "tool_events": state.get("tool_events", []),
                        "citations": state.get("citations", []),
                        "error_code": state.get("error_code"),
                        "llm_tokens": state.get("llm_tokens", 0),
                        "answer_summary": state.get("final_answer", "")[:160],
                        "recorded_at": datetime.now(UTC).isoformat(),
                    },
                )
            except Exception:
                # Observability failure cannot grant access or change the result.
                pass

    async def retry_outbox_once(self) -> None:
        for item in self.store.due_outbox():
            payload = item["payload"]
            principal = Principal(payload["principal_id"], payload["tenant_id"], ("orders:read", "tickets:write"))
            try:
                result = await self.tools.create_ticket(
                    payload["order_id"],
                    payload["issue_type"],
                    payload["summary"],
                    item["action_id"],
                    principal,
                )
                self.store.resolve_outbox(item["pending_ticket_id"], item["action_id"], result)
                TICKET_CREATED.inc()
            except ToolError as exc:
                if exc.code in {"UPSTREAM_RATE_LIMIT", "UPSTREAM_TIMEOUT", "UPSTREAM_ERROR"}:
                    self.store.postpone_outbox(item["pending_ticket_id"], item["attempts"] + 1, exc.code)
                else:
                    self.store.fail_outbox(item["pending_ticket_id"], item["action_id"], exc.code)

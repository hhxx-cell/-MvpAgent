"""LangGraph 中传递的最小状态。"""

from typing import Any, TypedDict

from agent.guards.authorization import Principal
from agent.router import Route


class AgentState(TypedDict, total=False):
    conversation_id: str
    message: str
    action: dict[str, str] | None
    principal: Principal
    trace_id: str
    route: Route
    order_id: str | None
    retrieval: Any
    sql_rows: list[dict[str, Any]]
    sql_template: str | None
    sql_attempts: int
    tool_result: dict[str, Any]
    tool_events: list[dict[str, str]]
    pending_action: dict[str, str] | None
    citations: list[dict[str, str]]
    warnings: list[str]
    error_code: str | None
    final_answer: str
    spans: list[dict[str, Any]]
    llm_tokens: int

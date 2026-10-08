"""低基数 Prometheus 指标；用户和订单不作为标签。"""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["route", "status"])
HTTP_DURATION = Histogram("http_request_duration_seconds", "HTTP duration", ["route"])
AGENT_ROUTE = Counter("agent_route_total", "Agent routes", ["intent"])
RAG_DURATION = Histogram("rag_retrieval_duration_seconds", "RAG duration")
RAG_NO_EVIDENCE = Counter("rag_no_evidence_total", "RAG without usable evidence")
SQL_REJECTED = Counter("sql_validation_rejected_total", "SQL rejected", ["reason"])
SQL_TEMPLATE_FALLBACK = Counter("sql_template_fallback_total", "Trusted SQL template fallbacks")
SQL_DURATION = Histogram("sql_execution_duration_seconds", "SQL execution duration")
SQL_CORRECTION = Counter("sql_correction_total", "SQL corrections")
TOOL_CALLS = Counter("tool_calls_total", "Tool calls", ["tool", "status"])
TOOL_RETRIES = Counter("tool_retry_total", "Tool retries", ["tool", "reason"])
TOOL_FAILURES = Counter("tool_failure_total", "Tool failures", ["tool", "reason"])
SSE_ACTIVE = Gauge("sse_connections_active", "Active SSE connections")
LLM_TOKENS = Counter("llm_tokens_total", "Model tokens", ["provider"])
TICKET_CREATED = Counter("ticket_created_total", "Created tickets")
TICKET_IDEMPOTENCY = Counter("ticket_idempotency_hit_total", "Idempotency hits")

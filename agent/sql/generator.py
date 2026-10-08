"""Narrow SQL candidate generation and schema linking.

The templates cover the MVP statistics and historical Join questions without
needing a model credential. A model gateway may supply another candidate, but
the caller must still pass it through validate_sql before execution.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .schema_catalog import SchemaCatalog

_CHINA_TIME = timezone(timedelta(hours=8))
_ORDER_ID = re.compile(r"(?:订单[号]?\s*[:：]?\s*)([A-Za-z][A-Za-z0-9_-]{3,31})", re.I)


class UnsupportedSQLQuestion(ValueError):
    """The question is outside the safe built-in query templates."""


@dataclass(frozen=True)
class SQLCandidate:
    sql: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    source: str = "template"


def linked_tables(question: str) -> set[str]:
    tables = {"orders"}
    if any(word in question for word in ("物流", "运单", "配送", "快递")):
        tables.add("logistics")
    if any(word in question for word in ("商品", "鞋", "品类", "SKU", "sku")):
        tables.add("products")
    return tables


def _time_filter(question: str, now: datetime) -> tuple[str, dict[str, str]]:
    local = now.astimezone(_CHINA_TIME)
    current_start = local.date().replace(day=1)
    if "上个月" in question or "上月" in question:
        end = current_start
        start = (end - timedelta(days=1)).replace(day=1)
    elif "这个月" in question or "本月" in question:
        start = current_start
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        return "", {}
    return (
        " AND o.created_at >= :start_date AND o.created_at < :end_date",
        {"start_date": start.isoformat(), "end_date": end.isoformat()},
    )


def generate_sql(
    question: str,
    catalog: SchemaCatalog,
    *,
    now: datetime | None = None,
    candidate_provider: Callable[[str, str], SQLCandidate | str] | None = None,
) -> SQLCandidate:
    """Generate one candidate with bound business values.

    A provider receives only the linked schema summary. It never receives a
    connection, trusted principal or authorization values. Its output remains
    untrusted until validate_sql succeeds.
    """
    if not question or not question.strip():
        raise UnsupportedSQLQuestion("Question is empty")
    if candidate_provider is not None:
        offered = candidate_provider(question, catalog.schema_summary(linked_tables(question)))
        if isinstance(offered, SQLCandidate):
            return SQLCandidate(offered.sql, dict(offered.parameters), source="provider")
        if isinstance(offered, str):
            return SQLCandidate(offered, {}, source="provider")
        raise UnsupportedSQLQuestion("Candidate provider returned an invalid response")

    now = now or datetime.now(_CHINA_TIME)
    time_sql, time_params = _time_filter(question, now)
    found_order = _ORDER_ID.search(question)
    order_id = found_order.group(1) if found_order else None
    if order_id and any(word in question for word in ("物流", "运单", "配送", "快递")):
        return SQLCandidate(
            "SELECT o.order_id, o.status AS order_status, o.amount_cents, "
            "l.logistics_id, l.status AS logistics_status, l.updated_at AS logistics_updated_at "
            "FROM orders AS o LEFT JOIN logistics AS l ON l.order_id = o.order_id "
            "WHERE o.order_id = :order_id ORDER BY l.updated_at DESC LIMIT 20",
            {"order_id": order_id},
        )
    if order_id:
        return SQLCandidate(
            "SELECT o.order_id, o.amount_cents, o.currency, o.status, o.created_at, o.updated_at "
            "FROM orders AS o WHERE o.order_id = :order_id LIMIT 1",
            {"order_id": order_id},
        )
    if any(
        word in question
        for word in (
            "消费",
            "花了多少",
            "花了",
            "花多少钱",
            "花费",
            "金额",
            "总额",
            "买了多少钱",
        )
    ):
        # Status semantics come from the shared catalog, never a guessed code.
        # Cancelled and refunded orders do not count as spent in this dataset.
        spent_labels = {"已支付", "已发货", "已完成", "退款处理中", "退款失败"}
        status_codes = catalog.status_codes.get("orders", {})
        spent_codes = sorted(code for code, label in status_codes.items() if label in spent_labels)
        if len(spent_codes) != len(spent_labels):
            raise UnsupportedSQLQuestion("Order status dictionary is incomplete")
        status_params = {f"spent_status_{index}": code for index, code in enumerate(spent_codes)}
        status_sql = " AND o.status IN (" + ", ".join(f":{name}" for name in status_params) + ")"
        return SQLCandidate(
            "SELECT COALESCE(SUM(o.amount_cents), 0) AS total_cents, "
            "COUNT(o.order_id) AS order_count FROM orders AS o WHERE 1 = 1" + status_sql + time_sql,
            {**status_params, **time_params},
        )
    if any(
        word in question
        for word in (
            "多少订单",
            "订单数量",
            "几笔订单",
            "多少笔",
            "购买次数",
            "订单数",
        )
    ):
        return SQLCandidate(
            "SELECT COUNT(o.order_id) AS order_count FROM orders AS o WHERE 1 = 1" + time_sql,
            time_params,
        )
    raise UnsupportedSQLQuestion("No safe template matches this question")

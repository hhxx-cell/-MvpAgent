"""可审计的结构化意图识别；不可从用户文本获得安全上下文。"""

import re
from dataclasses import dataclass, field

POLICY_TERMS: dict[str, tuple[str, ...]] = {
    "return_policy": ("退货", "退换货", "七天无理由"),
    "refund_policy": ("退款政策", "退款规则", "退款时效", "退款多久"),
    "invoice_policy": ("发票", "开票"),
    "shipping_policy": ("运费", "包邮", "发货规则"),
    "warranty_policy": ("保修", "质保"),
    "exchange_policy": ("换货", "换新"),
    "price_protection": ("价保", "价格保护", "保价"),
    "damaged_package": ("破损", "包装损坏"),
    "delivery_delay": ("延迟送达", "配送延迟", "物流延误"),
    "aftersales_ticket": ("工单规则", "售后工单政策"),
}

ORDER_ID = re.compile(r"(?<![A-Za-z0-9])O\d{5,}(?![A-Za-z0-9])", re.IGNORECASE)
TICKET_WORDS = ("建单", "建工单", "创建工单", "创建售后工单", "提交工单", "联系售后", "发起工单")
LOGISTICS_WORDS = ("物流", "配送", "快递", "运到", "到哪", "运单")
STATISTICS_WORDS = ("消费", "总金额", "总额", "多少钱", "多少笔", "统计", "平均", "购买次数", "金额", "join", "关联")
ORDER_WORDS = ("订单", "退款进度", "退款状态", "当前状态", "买的东西")


@dataclass(frozen=True)
class Route:
    intents: tuple[str, ...] = field(default_factory=tuple)
    order_id: str | None = None
    policy_key: str | None = None
    clarification: str | None = None
    sql_kind: str | None = None


def classify(message: str, last_order_id: str | None = None) -> Route:
    text = message.strip()
    if not text or len(text) > 4000:
        return Route(clarification="请简要说明您要查询的售后问题。")
    match = ORDER_ID.search(text)
    order_id = match.group(0).upper() if match else last_order_id
    policy_key = next((key for key, terms in POLICY_TERMS.items() if any(term in text for term in terms)), None)
    ticket = any(word in text for word in TICKET_WORDS)
    logistics = any(word in text for word in LOGISTICS_WORDS)
    statistics = any(word.lower() in text.lower() for word in STATISTICS_WORDS)
    order = any(word in text for word in ORDER_WORDS) or bool(match)
    # Pure policy requests should not unexpectedly call an order tool.
    pure_policy = (
        policy_key is not None
        and not match
        and not ticket
        and not statistics
        and not (logistics and ("我的" in text or "订单" in text))
    )
    intents: list[str] = []
    if policy_key:
        intents.append("rag")
    if ticket:
        intents.append("ticket")
    elif statistics:
        intents.append("sql")
    elif logistics and not pure_policy:
        intents.append("logistics")
    elif order and not pure_policy:
        intents.append("order")
    if not intents:
        return Route(clarification="请说明您要了解政策、查询订单或统计消费中的哪一项。")
    if any(intent in intents for intent in ("order", "logistics", "ticket")) and not order_id:
        return Route(tuple(intents), policy_key=policy_key, clarification="请提供订单号，例如 O00001。")
    sql_kind = None
    if "sql" in intents:
        if logistics and "订单" in text:
            sql_kind = "order_logistics"
        elif order_id:
            sql_kind = "order_amount"
        elif "多少笔" in text or "购买次数" in text:
            sql_kind = "order_count"
        else:
            sql_kind = "spend_total"
    return Route(tuple(intents), order_id, policy_key, sql_kind=sql_kind)

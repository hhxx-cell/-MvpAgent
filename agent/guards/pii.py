"""日志、Trace 与 SSE 共用的脱敏函数。"""

import re
from typing import Any

BEARER = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
ORDER_ID = re.compile(r"\bO\d{5,}\b", re.IGNORECASE)
SECRET = re.compile(r"(?i)(api[_-]?key|token|password)\s*[:=]\s*[^\s,;]+")


def redact_text(value: str, *, mask_order: bool = True) -> str:
    value = BEARER.sub("Bearer [REDACTED]", value)
    value = PHONE.sub("[PHONE]", value)
    value = EMAIL.sub("[EMAIL]", value)
    value = SECRET.sub(r"\1=[REDACTED]", value)
    if mask_order:
        value = ORDER_ID.sub("[ORDER_ID]", value)
    return value


def redact(value: Any, *, mask_order: bool = True) -> Any:
    if isinstance(value, str):
        return redact_text(value, mask_order=mask_order)
    if isinstance(value, list):
        return [redact(item, mask_order=mask_order) for item in value]
    if isinstance(value, dict):
        return {
            key: redact(item, mask_order=mask_order)
            for key, item in value.items()
            if key.lower() not in {"authorization", "cookie", "api_key", "password", "secret"}
        }
    return value

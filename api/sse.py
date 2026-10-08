"""单一固定格式的 SSE 序列化。"""

import json

from api.schemas import Event


def encode(event: Event) -> str:
    data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.event}\ndata: {data}\n\n"

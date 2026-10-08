"""公开请求和 SSE 事件的数据契约。"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ActionDecision(BaseModel):
    action_id: str = Field(pattern=r"^act_[0-9a-f]{32}$")
    decision: Literal["confirm", "cancel"]


class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(default="", max_length=4000)
    action: ActionDecision | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) - {"channel"}:
            raise ValueError("unsupported metadata key")
        if any(len(item) > 40 or not re.fullmatch(r"[\w-]+", item) for item in value.values()):
            raise ValueError("invalid metadata value")
        return value


class Event(BaseModel):
    event: Literal[
        "step.started", "tool.started", "tool.completed", "action.preview", "warning", "message.delta", "done", "error"
    ]
    data: dict[str, Any]

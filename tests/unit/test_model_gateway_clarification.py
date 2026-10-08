"""Requests missing an order ID should clarify without waiting for a model call."""

from unittest.mock import AsyncMock, patch

import pytest

from agent.config import Settings
from agent.model_gateway import OpenAICompatibleGateway


def gateway() -> OpenAICompatibleGateway:
    return OpenAICompatibleGateway(
        Settings(
            _env_file=None,
            app_env="test",
            llm_provider="openai_compatible",
            llm_base_url="https://model.example/v1",
            llm_model="test-model",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("请查一下订单状态。", "order"),
        ("帮我看物流。", "logistics"),
        ("帮我建工单。", "ticket"),
    ],
)
async def test_missing_order_id_clarifies_without_model_call(question: str, intent: str) -> None:
    model = gateway()
    with patch.object(model, "_json_completion", side_effect=AssertionError("model must not be called")) as completion:
        route, tokens = await model.classify(question, None)

    assert route.intents == (intent,)
    assert route.clarification == "请提供订单号，例如 O00001。"
    assert tokens == 0
    completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_id_request_still_uses_model() -> None:
    model = gateway()
    with patch.object(model, "_json_completion", new_callable=AsyncMock) as completion:
        completion.return_value = ({"intents": ["logistics"]}, 19)
        route, tokens = await model.classify("查询订单 O00001 的物流", None)

    assert route.intents == ("logistics",)
    assert route.order_id == "O00001"
    assert route.clarification is None
    assert tokens == 19
    completion.assert_awaited_once()

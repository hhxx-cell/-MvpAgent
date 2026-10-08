"""The same compatible protocol works for public and local model URLs."""

import json
from unittest.mock import patch

import httpx
import pytest

from agent.config import Settings
from agent.model_gateway import OpenAICompatibleGateway, create_gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["https://model.example/v1", "http://local-model:8000/v1"])
async def test_config_switch_uses_compatible_route_and_sql_contract(base_url: str) -> None:
    requests: list[httpx.Request] = []
    template = "SELECT SUM(total_amount) FROM orders WHERE principal_id = :auth_principal_id"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        if "SQLite" in payload["messages"][0]["content"]:
            content = {"sql": template}
        else:
            content = {"intents": ["sql"], "sql_kind": "spend_total"}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}], "usage": {"total_tokens": 17}},
        )

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_provider="openai_compatible",
        llm_base_url=base_url,
        llm_model="compatible-test-model",
        llm_api_key="test-placeholder",
    )
    with patch("agent.model_gateway.httpx.AsyncClient", side_effect=client_factory):
        gateway = create_gateway(settings)
        assert isinstance(gateway, OpenAICompatibleGateway)
        route, route_tokens = await gateway.classify("我上个月消费了多少元？", None)
        sql, sql_tokens = await gateway.generate_sql(
            "我上个月消费了多少元？", "orders(principal_id, total_amount)", template
        )

    assert route.intents == ("sql",)
    assert route.sql_kind == "spend_total"
    assert (route_tokens, sql_tokens) == (17, 17)
    assert sql == template
    assert len(requests) == 2
    for request in requests:
        assert str(request.url) == base_url + "/chat/completions"
        assert request.headers["authorization"] == "Bearer test-placeholder"
        payload = json.loads(request.content)
        assert payload["model"] == "compatible-test-model"
        assert payload["response_format"] == {"type": "json_object"}

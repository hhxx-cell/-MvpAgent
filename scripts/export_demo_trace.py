"""用完全合成的订单导出一条实际 HTTP/SSE 调用链。"""

import asyncio
import html
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from agent.config import ROOT, Settings
from agent.tools.registry import ToolClient
from api.main import create_app
from mock.mock_server import create_app as create_mock_app

OUTPUT = ROOT / "docs" / "evidence" / "trace-demo.json"


def render_html(trace: dict) -> str:
    def safe(value: object) -> str:
        return html.escape(str(value))

    spans = "".join(
        f"<div class='span'><span class='dot'></span><div><strong>{safe(item['name'])}</strong>"
        f"<small>{safe(item['status'])} · {safe(item['duration_ms'])} ms</small></div></div>"
        for item in trace["spans"]
    )
    tools = "".join(
        f"<span class='chip'>{safe(item['tool'])} · {safe(item['status'])}</span>" for item in trace["tool_events"]
    )
    return f"""<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>
<title>合成数据调用链</title><style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f3f5f8;color:#182536;font:16px/1.5 system-ui,'Microsoft YaHei',sans-serif}}
.page{{max-width:1080px;margin:38px auto;padding:0 24px}}
h1{{font-size:32px;margin:0 0 6px}}
p{{margin:0 0 18px;color:#506176}}
.meta{{display:flex;gap:12px;margin:24px 0}}
.badge{{background:#e7f4ef;color:#176c48;padding:7px 12px;border-radius:999px;font-weight:600}}
.card{{background:white;border:1px solid #dce3eb;border-radius:15px;padding:22px 26px;margin:16px 0}}
h2{{font-size:18px;margin:0 0 16px}}
.label{{color:#62758a;font-size:13px;text-transform:uppercase;letter-spacing:.06em}}
.value{{font-size:17px;margin:4px 0 16px;overflow-wrap:anywhere}}
.span{{display:flex;align-items:center;gap:14px;padding:9px 0;border-left:2px solid #dbe6f0;margin-left:9px}}
.dot{{width:16px;height:16px;background:#377dca;border:3px solid white;box-shadow:0 0 0 2px #377dca}}
.dot{{border-radius:50%;margin-left:-9px}}
.span strong{{display:block}}
.span small{{display:block;color:#62758a}}
.chip{{display:inline-block;background:#eef3fa;border-radius:8px;padding:7px 10px;margin:0 8px 8px 0}}
.chip{{font-size:14px}}
.answer{{font-size:18px;line-height:1.7}}
footer{{color:#728298;font-size:13px;margin:24px 0}}
</style></head><body><main class='page'><h1>智能售后 Agent 调用链</h1>
<p>合成订单演示 · 来自受保护 Trace 导出 · 敏感字段已脱敏</p>
<div class='meta'><span class='badge'>状态：成功</span>
<span class='badge'>路由：{safe(", ".join(trace["route"]))}</span>
<span class='badge'>Token：{safe(trace["llm_tokens"])}</span></div>
<section class='card'><h2>请求与回答</h2><div class='label'>脱敏请求</div>
<div class='value'>{safe(trace["prompt_summary"])}</div>
<div class='label'>结果摘要</div><div class='answer'>{safe(trace["answer_summary"])}</div></section>
<section class='card'><h2>执行步骤</h2>{spans}</section>
<section class='card'><h2>工具调用</h2>{tools}</section>
<footer>Trace ID：{safe(trace["trace_id"])} · 导出时间：{safe(trace["recorded_at"])}</footer></main></body></html>"""


async def export() -> None:
    with TemporaryDirectory() as directory:
        tmp = Path(directory)
        settings = Settings(
            app_env="test",
            database_path=ROOT / "database" / "ecommerce.db",
            knowledge_path=ROOT / "knowledge" / "source",
            state_db_path=tmp / "state.db",
            qdrant_url=":memory:",
            mock_server_url="http://mock-server",
            mock_internal_key="trace-demo-key",
        )
        mock = create_mock_app(settings, ticket_db_path=tmp / "tickets.db")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock), base_url="http://mock-server"
        ) as mock_http:
            api = create_app(settings, tool_client=ToolClient(settings, client=mock_http))
            async with api.router.lifespan_context(api):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://api") as client:
                    response = await client.post(
                        "/chat",
                        headers={"Authorization": "Bearer demo-user-a"},
                        json={"conversation_id": "c_trace_demo", "message": "订单 O00001 的退款进度"},
                    )
                    response.raise_for_status()
                    done = next(
                        json.loads(line.removeprefix("data: "))
                        for block in response.text.split("\n\n")
                        if block.startswith("event: done\n")
                        for line in block.splitlines()
                        if line.startswith("data: ")
                    )
                    trace_response = await client.get(
                        f"/internal/traces/{done['trace_id']}",
                        headers={"Authorization": "Bearer demo-admin"},
                    )
                    trace_response.raise_for_status()
                    trace = trace_response.json()
                    rendered = json.dumps(trace, ensure_ascii=False, indent=2)
                    assert "O00001" not in rendered and "demo-user-a" not in rendered
                    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
                    OUTPUT.write_text(rendered + "\n", encoding="utf-8")
                    OUTPUT.with_suffix(".html").write_text(render_html(trace), encoding="utf-8")
                    print(OUTPUT)


if __name__ == "__main__":
    asyncio.run(export())

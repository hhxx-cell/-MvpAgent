# 自建售后 Mock 服务

服务从 `database/ecommerce.db` 只读查询合成订单和物流，将工单保存到独立的
`runtime/mock_tickets.db`。与 Agent 使用同一份生成数据和状态字典。

启动前运行：

```powershell
.\.venv\Scripts\python.exe scripts\generate_seed_data.py
.\.venv\Scripts\python.exe -m uvicorn mock.mock_server:app --host 127.0.0.1 --port 8081
```

除 `/health` 外，每个请求必须带内部密钥和可信身份：

```text
X-Internal-Service-Key: local-demo-internal-key
X-Principal-ID: user_a
X-Tenant-ID: tenant_demo
```

通过 `MOCK_INTERNAL_KEY` 更换密钥。生产环境禁止使用默认演示密钥。服务仅接受
`user_a` 或 `user_b` 的订单请求，跨用户订单统一返回 404。

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 数据库和工单库就绪检查 |
| `GET /orders/{order_id}` | 查询当前身份的订单 |
| `GET /orders?page=1&page_size=20` | 分页查询当前身份的订单 |
| `GET /orders/{order_id}/logistics` | 先校验订单归属，再返回物流 |
| `POST /tickets` | 建立售后工单，要求 `Idempotency-Key` |

`POST /tickets` 请求体：

```json
{
  "order_id": "O00001",
  "issue_type": "refund_failure",
  "summary": "退款状态异常，请核实",
  "action_id": "act_example_001"
}
```

Agent 层须在调用工单接口前完成预览和确认。Mock 服务再次验证订单归属；同一
`Idempotency-Key` 与相同请求只返回同一工单，密钥复用到不同请求则返回 409。
工单号仅在成功写入后返回。

开发或测试环境设置 `MOCK_SCENARIO_CONTROL_ENABLED=true` 后，可以带
`X-Mock-Scenario: rate_limit`、`timeout` 或 `server_error` 注入故障；参数来自
`scenarios.yaml`。生产环境不接受场景头。`rate_limit` 固定返回 429 和
`Retry-After: 1`，`timeout` 延迟 6 秒，`server_error` 固定返回 500。

订单接口响应包含 `status: success`、`source: mock_server`、`observed_at`
和 `order` 对象；分页还包含 `items`、`page`、`page_size`、`total`、
`next_page`。物流接口返回 `logistics` 对象。订单和物流状态都是数字码，
同时附带来自 `schema_catalog.yaml` 的中文 `status_label`。

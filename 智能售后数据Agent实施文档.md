# 智能售后数据 Agent 实施文档

## 1 文档目标

本文档给出“智能售后数据 Agent”的可执行实施方案。系统需要在同一会话中完成政策问答、数据库查询和售后业务办理，并满足只读 SQL、安全授权、失败降级、流式响应、可观测、容器化和评测要求。

实施基线以 MVP 为先。缓存、图表、长会话压缩、离线模型和完整评测闭环在核心链路稳定后逐步启用，但架构从第一版开始保留扩展接口。

已确认题目中列出的 knowledge、ecommerce.db、orders.jsonl、logs.jsonl 和 mock_server.py 均未实际提供。它们全部作为项目交付物自行生成，并使用固定随机种子保证测试和演示可复现。

## 2 实施范围

### 2.1 MVP 必须交付

1. 自动识别政策问答、数据查询、实时订单或物流查询、工单办理和缺参场景。
2. 使用自建向量库检索售后知识，回答中给出文档名、段落标识和生效日期。
3. 对 SQLite 执行受控 Text to SQL，支持多表 Join、明确字段选择和真实数值回答。
4. 提供 query_order、query_logistics、create_ticket 三个工具，并支持多步调用。
5. 对缺少订单号等必要参数的请求先澄清，不猜测参数。
6. 拦截 SQL 注入、非只读语句、越权查询和知识库中的 Prompt 注入。
7. 对可修复 SQL 错误最多进行有限次数纠错；对 mock 服务的 429、超时和 5xx 执行退避重试和降级。
8. 通过 FastAPI 提供 /chat、/health、/metrics，其中 /chat 使用 SSE。
9. 记录请求、路由、检索、SQL、工具、耗时和 token 信息，能够展示一条完整调用链。
10. 使用 Docker Compose 拉起 Agent、Qdrant 和项目自建的 mock 服务。
11. 提供不少于 8 条 pytest 单元测试、黑盒评测脚本和中文 README。

### 2.2 第二阶段能力

- 基于自行生成的 logs.jsonl 建立不少于 30 条 golden 用例，输出 Answer Relevancy、Faithfulness 和 Tool call Accuracy。
- Redis 结果缓存和语义缓存。
- 月度消费趋势等图表生成。
- 长对话摘要压缩。
- Qwen3 8B 或 GLM 等本地模型切换，以及内网离线部署。
- 部署手册、FAQ、风险清单和资源估算。

### 2.3 当前不纳入 MVP

- 修改收货地址。业务背景提到该能力，但 MVP 工具清单未要求对应工具，需在接口和授权规则明确后单独实施。
- 未经确认的写操作。create_ticket 是唯一允许的业务写操作，其他数据库和外部系统操作默认拒绝。
- 直接输出模型的隐式推理链。SSE 只输出可审计的步骤状态、工具调用和结果摘要，避免泄露系统提示词、安全规则或敏感数据。

### 2.4 自建数据资产基线

| 资产 | 自建要求 |
| --- | --- |
| knowledge | 生成约 30 篇 Markdown 售后文档，其中至少 5 篇为同主题旧版本或冲突版本；每篇包含 effective_date、主题和稳定段落编号 |
| ecommerce.db | 按题目列出的 orders、logistics、products 三张核心表建库，并从统一数据清单导入有效记录 |
| orders.jsonl | 生成 200 条订单样例，覆盖正常、缺失字段、未知状态、重复 ID 和分页边界；异常记录用于校验与负向测试 |
| logs.jsonl | 生成 50 条完全合成的多轮售后对话，不使用真实客服记录或真实个人信息 |
| mock_server.py | 自行实现订单、物流、工单及故障注入接口，稳定复现 429、超时和 500 |

生成器统一接收固定 MOCK_RANDOM_SEED，并输出 data_manifest.json，记录版本、生成参数、文件哈希、记录数和异常样本清单。CI 每次基于同一 seed 重建后校验哈希，避免测试数据漂移。

## 3 技术选型

| 领域 | 选择 | 实施理由 |
| --- | --- | --- |
| Agent 编排 | LangGraph | 适合显式状态、条件分支、重试和多工具流程，便于测试每个节点 |
| API | FastAPI 与 uvicorn | 原生支持异步接口、SSE、Pydantic 数据校验和健康检查 |
| 向量库 | Qdrant 自托管 | 容器化简单，支持元数据过滤，适合按政策类别和生效日期检索 |
| 业务数据库 | SQLite 只读连接 | 与题目数据一致；通过 URI 只读模式和 query_only 双重限制 |
| SQL 解析 | sqlglot | 使用 AST 校验语句类型、表、列、函数和查询结构 |
| 模型接入 | ModelGateway 抽象 | 公网 API 与本地 vLLM 或 llama.cpp 使用同一调用接口，通过配置切换 |
| 外部调用 | httpx AsyncClient | 支持异步超时、连接池、重试控制和取消 |
| 观测 | 本地 TraceStore 加可选 Langfuse Exporter | 核心 Compose 即可保存和查看完整链路；启用 Langfuse 时异步导出 |
| 指标 | prometheus client | 为 /metrics 暴露请求量、延迟、错误、重试和工具调用指标 |
| 配置 | pydantic settings | 从 .env 和环境变量加载并验证配置，启动时快速失败 |
| 测试 | pytest、pytest asyncio | 覆盖图节点、SQL 安全、工具失败、SSE 和端到端链路 |

依赖版本统一锁定在 pyproject.toml 或 requirements.txt 中。模型、向量模型、超时和重试参数不得散落在代码里。

## 4 总体架构

~~~mermaid
flowchart LR
    U[客服或终端用户] --> API[FastAPI 与 SSE]
    API --> AUTH[身份与会话上下文]
    AUTH --> G[LangGraph Agent]

    G --> R{意图与路由}
    R -->|政策问题| RAG[RAG 检索与版本裁决]
    R -->|统计或关系查询| SQL[Text to SQL 安全管线]
    R -->|实时订单与物流| TOOL[业务工具层]
    R -->|缺少必要参数| CLARIFY[澄清节点]

    RAG --> Q[(Qdrant)]
    RAG --> K[Markdown 知识库]
    SQL --> DB[(SQLite 只读)]
    TOOL --> MOCK[项目自建 mock 服务]
    TOOL --> TICKET[工单工具]

    RAG --> ANSWER[事实校验与答案生成]
    SQL --> ANSWER
    TOOL --> ANSWER
    CLARIFY --> ANSWER
    ANSWER --> API

    G -. trace .-> OBS[本地 TraceStore 与可选 Langfuse]
    API -. metrics .-> METRICS[/metrics]
    G -. fallback log .-> LOG[结构化日志]
~~~

### 4.1 请求处理原则

- 静态政策由 RAG 回答，动态数据必须从数据库或业务工具获取。
- 需要用户身份的数据查询在进入 Agent 前完成认证，并将 user_id 作为不可由模型修改的可信上下文。
- 生成式模型负责理解、路由和组织答案，不直接获得数据库连接或任意网络访问权限。
- 所有外部动作都经过显式工具定义、参数校验、权限校验和审计。
- 任一关键安全检查失败时立即停止当前路径，不允许模型通过重试绕过策略。

## 5 代码与交付目录

~~~text
/agent
  graph.py
  state.py
  router.py
  prompts/
  retriever/
    ingest.py
    service.py
    versioning.py
  sql/
    schema_catalog.py
    generator.py
    validator.py
    executor.py
    correction.py
  tools/
    order.py
    logistics.py
    ticket.py
    registry.py
  guards/
    prompt_injection.py
    authorization.py
    pii.py
  memory/
    conversation.py
    summarizer.py
  observability/
    tracing.py
    metrics.py
/api
  main.py
  routes_chat.py
  routes_health.py
  routes_metrics.py
  sse.py
  schemas.py
/eval
  logs.jsonl
  golden.jsonl
  eval.py
  报告.md
/knowledge
  source/
  ingest_manifest.json
  入库脚本.py
/database
  ecommerce.db
  schema.sql
  schema_catalog.yaml
  seeds/
    orders.jsonl
/mock
  mock_server.py
  scenarios.yaml
  README.md
/scripts
  generate_seed_data.py
  verify_data_manifest.py
/docker
  Dockerfile
  docker-compose.yml
  docker-compose.offline.yml
/tests
  unit/
  integration/
  e2e/
/docs
  部署手册.md
  架构设计.md
  交付清单.md
  FAQ.md
  已知问题与风险.md
  资源与报价估算.md
README.md
pyproject.toml
.pre-commit-config.yaml
.env.example
.dockerignore
~~~

## 6 核心数据契约

### 6.1 聊天请求

~~~json
{
  "conversation_id": "c_20260923_001",
  "message": "帮我查一下订单 12345 的退款进度",
  "action": null,
  "metadata": {
    "channel": "web"
  }
}
~~~

- principal_id、tenant_id 和 scopes 必须由认证层从令牌或会话生成，不放入可被客户端任意修改的请求字段。
- conversation_id 用于关联上下文、幂等键和 trace。
- action 仅用于提交上一轮 action.preview 的确认或取消，格式为 action_id 与 decision；服务端按 action_id 读取参数快照，不接受客户端重传工具参数。
- metadata 只能保存经过白名单校验的非敏感字段。

### 6.2 Agent 状态

~~~python
class PrincipalContext(TypedDict):
    principal_id: str
    tenant_id: str
    scopes: list[str]


class AgentState(TypedDict):
    conversation_id: str
    principal: PrincipalContext
    messages: list
    intent: str | None
    required_slots: dict
    retrieved_chunks: list
    sql_candidate: str | None
    sql_attempts: int
    query_result: list
    tool_events: list
    pending_action: dict | None
    warnings: list
    final_answer: str | None
    trace_id: str
~~~

PrincipalContext、pending_action 中的确认状态和安全判定由认证中间件或策略层写入。客户端和模型均不能创建或覆盖这些可信字段。

### 6.3 种子数据处理

- scripts/generate_seed_data.py 以固定 seed 生成知识文档、订单、物流、商品、合成对话和故障场景，禁止手工维护互相不一致的副本。
- orders.jsonl 是订单原始样例；通过字段类型、必填项、重复 ID、状态枚举和时间格式校验的记录才进入 ecommerce.db。
- 缺失字段、未知状态和重复 ID 记录进入 rejected_orders.jsonl 与数据质量报告，不用默认值掩盖。
- schema_catalog.yaml 定义订单和物流状态字典，生成器与应用共享同一份映射，模型不得猜测状态码。
- 分页通过 mock API 的 page、page_size、total 和 next_page 契约验证，不把 JSONL 文件行号当成分页语义。
- SQLite、JSONL、mock API 和 golden 用例均引用同一组 order_id、user_id 和时间基线；verify_data_manifest.py 检查跨文件引用完整性。
- 默认金额币种为 CNY，金额使用两位小数的 Decimal 语义，业务时区为 Asia/Shanghai，并写入数据清单。
- 合成身份至少包含两个普通用户，用于验证用户 A 无法访问用户 B 的订单。
- 种子数据仅用于初始化和测试，禁止在 Prompt 或业务代码中硬编码问题答案。
- 禁止安装或套用现成“电商客服 Agent”项目；允许使用通用框架和基础库，但业务路由、安全策略和评测必须自行实现。

### 6.4 自建 mock 服务契约

mock_server.py 使用 FastAPI 实现，并至少提供以下接口：

| 接口 | 用途 |
| --- | --- |
| GET /orders/{order_id} | 返回实时订单状态，并验证可信用户上下文 |
| GET /orders | 分页返回当前用户订单，支持 page 和 page_size |
| GET /orders/{order_id}/logistics | 返回物流节点与更新时间 |
| POST /tickets | 使用 action_id 和幂等键创建工单 |
| GET /health | 返回 mock 服务就绪状态 |

测试环境通过 X-Mock-Scenario 请求头或 scenarios.yaml 选择 rate_limit、timeout、server_error 和 success 场景。该入口只在 test 或 development 环境启用；生产适配器不接受故障注入参数。

SQLite 是统计和历史查询的数据源，mock API 是实时订单、物流和工单操作的数据源。两者由同一 data_manifest.json 生成，若相同订单的基础字段不一致，CI 直接失败。

## 7 Agent 工作流

### 7.1 图节点

| 节点 | 输入 | 处理 | 输出或下一步 |
| --- | --- | --- | --- |
| authenticate | HTTP 请求 | 验证身份并建立数据访问范围 | 失败返回 401 或 403 |
| classify | 用户问题与短期上下文 | 输出结构化意图和所需槽位 | clarify、rag、sql 或 tools |
| clarify | 缺失槽位 | 生成一个明确问题 | 结束本轮并等待用户补充 |
| retrieve | 政策问题 | 检索、去重、版本裁决 | evidence |
| generate_sql | 数据问题与相关 Schema | 生成单条候选查询 | validate_sql |
| validate_sql | SQL AST 与授权上下文 | 执行静态安全检查并注入权限过滤 | execute_sql 或拒绝 |
| execute_sql | 已验证 SQL 与参数 | 只读执行、限时、限行 | rows 或受控错误 |
| correct_sql | 可修复数据库错误 | 基于错误类别修正 SQL | validate_sql，最多两次 |
| call_tool | 已验证工具名与参数 | 调用 mock 服务或创建工单 | tool_result |
| verify | evidence、rows 或工具结果 | 校验答案中的事实和来源 | compose |
| compose | 可信数据 | 生成简洁答案和引用 | stream |
| fallback | 连续外部失败 | 告知当前状态并提出工单选项，等待用户确认 | stream |

### 7.2 路由规则

- “退货政策”“发票规则”等不依赖用户实时数据的问题进入 RAG。
- “上个月买了多少钱”“订单金额是多少”等统计或关系问题进入 Text to SQL。
- 订单当前状态和物流节点优先调用实时工具；数据库可用于补充分析，但不能用历史快照冒充实时结果。
- 一个问题同时包含政策和订单事实时，两个分支分别取证，再由 compose 节点合并。
- 创建工单、未来可能加入的修改地址等写操作必须经过明确的工具节点，不能由 SQL 实现。
- 缺少订单号、时间范围或其他必要参数时直接进入 clarify，不调用工具、不生成猜测 SQL。
- 路由模型必须输出受 Pydantic 校验的枚举和槽位；解析失败时重试一次，之后按安全默认策略澄清。

### 7.3 异常订单处理

当订单或 SQL 结果显示“退款失败”“长时间未更新”等异常状态时：

1. 先向用户说明查到的状态和数据时间。
2. 询问是否需要创建售后工单。
3. 用户确认后调用 create_ticket。
4. 返回工单号、创建结果和后续查询方式。
5. 使用一次性 action_id 生成幂等键，避免 SSE 重连、重复确认或模型重试造成重复建单。

## 8 RAG 实施

### 8.1 知识入库

入库脚本按以下顺序处理 knowledge 目录：

1. 解析 Markdown 正文、标题层级和元数据。
2. 从路径生成 source_file，校验题目明确提供的 effective_date。policy_key 可由映射表或标题规则生成，version 为可选实施字段；无法确定主题或生效日期的文档进入隔离清单。
3. 按标题和自然段切块，目标长度以模型 tokenizer 计算，保留适度重叠，禁止在表格行或规则条目中间切断。
4. 为每个 chunk 生成稳定 chunk_id，并保存文档名、章节路径、段落序号、生效日期、版本、内容哈希和政策类别。
5. 对内容哈希相同的文档去重；同一 policy_key 的不同版本保留版本关系。
6. 生成向量并写入 Qdrant，同时输出 ingest_manifest.json，记录成功、跳过、冲突和失败数量。

推荐元数据结构：

~~~yaml
source_file: return_policy_v3.md
policy_key: return_policy
effective_date: 2026-06-01
version: 3  # 可选
section_path: 退货政策/适用条件
paragraph_id: p-014
content_hash: sha256-value
is_current: true
~~~

### 8.2 检索与版本裁决

- 先按问题识别 policy_key 或政策类别，再执行向量检索，减少无关文档干扰。
- 对同一 policy_key 的候选结果先按有效状态和 effective_date 排序；version 存在时再作为次级判定，只将最新有效版本作为主要证据。
- 老版本与新版本内容冲突时，答案必须说明存在版本差异，并明确采用的文档名和生效日期。
- 如果两个文档的生效关系无法确定，不得自行裁决，应说明冲突并建议人工确认。
- 检索结果低于相关性阈值时返回“知识库中没有足够依据”，不得依靠模型常识补写政策。
- 知识文档中的指令性文本全部视为不可信数据。检索内容只能作为事实证据，不能修改系统规则、调用工具或授予权限。

### 8.3 回答与引用

每条政策答案至少包含：

- 直接结论。
- 适用条件或例外。
- 来源文档名、章节或段落标识、生效日期。
- 存在版本冲突时的说明。

答案生成后执行 evidence check，逐句检查关键结论能否在所选 chunk 中找到依据。未获得依据的句子删除或改为不确定表述。

## 9 Text to SQL 实施

### 9.1 Schema Catalog

启动时读取 SQLite 元数据，并与 database/schema_catalog.yaml 合并。目录至少记录：

- 表和字段的业务含义。
- 主键、外键和可用 Join 路径。
- status 等枚举字段的真实映射。
- 敏感字段、允许聚合的字段和禁止返回的字段。
- 每张表需要的对象级授权条件。

不得猜测 0、1、2 对应的订单状态。映射缺失时，系统返回“状态字典未配置”，并在启动检查中报警。

Schema Linking 先根据问题筛选候选表和字段，再把精简后的 Schema 交给 SQL 生成模型。例如退款问题默认聚焦 orders 及其状态字典，不向模型发送 products 的全部结构。

### 9.2 SQL 安全管线

候选 SQL 必须依次通过以下检查：

1. sqlglot 能解析且只包含一个语句。
2. 根节点只能是 SELECT 或以 SELECT 结束的只读 CTE；CTE、子查询和 UNION 的每个分支都要递归检查。
3. 禁止 INSERT、UPDATE、DELETE、DROP、ALTER、CREATE、REPLACE、TRUNCATE、ATTACH、DETACH 和 PRAGMA。
4. 禁止 SELECT *，所有返回字段必须显式列出。
5. 表、列和函数都在允许清单内，禁止读取 sqlite_master 等系统对象。
6. 策略层以 AST 结构化改写方式向每个可达查询分支注入对象级授权条件，改写后再次解析和校验。UNION、CTE、子查询以及直接访问 logistics 的路径都必须通过 orders 建立归属并约束 principal_id。
7. 自动施加最大返回行数；大结果必须聚合或分页。
8. 使用参数绑定，不拼接订单号、用户 ID、时间范围等用户输入。
9. 数据库连接使用 mode=ro，并执行 PRAGMA query_only=ON。
10. 设置执行超时或 SQLite progress handler，超时立即中止。

用户输入中即使包含 “DROP TABLE orders” 等文本，也只能作为不可信数据传给路由器和 SQL 生成器，绝不能直接拼接到 SQL。若生成 SQL 出现禁用节点，validator 直接拒绝，不进入纠错。

### 9.3 执行与事实回答

- executor 只接受 validator 产出的 SQL 对象，不接受原始字符串绕过。
- 查询结果先转换为带字段名的结构化数据，再交给答案节点。
- 金额、数量、日期和状态必须直接来自查询结果；空结果明确回答“未查到”，不得补全。
- 回答中保留必要单位和时间范围。
- Trace 可记录 SQL、执行耗时和有限的原始结果片段，但在写入前必须脱敏并限制行数。

### 9.4 自我纠错

仅对字段名错误、Join 别名错误、SQLite 方言差异等可修复错误进行纠错：

1. 将错误类别、数据库返回的安全错误摘要和相关 Schema 交给 correction 节点。
2. 生成新 SQL 后重新经过完整 validator。
3. 最多重试两次，且总执行时间受请求截止时间限制。
4. 权限错误、安全违规、超时和资源上限错误不得通过模型重试。
5. 连续失败后向用户说明暂时无法完成查询，并记录 trace。

## 10 工具层与失败降级

### 10.1 工具契约

| 工具 | 模型可见业务参数 | 返回 | 服务端安全注入与校验 |
| --- | --- | --- | --- |
| query_order | order_id | 订单状态、金额、创建时间、数据时间 | 注入 PrincipalContext，验证订单归属 |
| query_logistics | order_id | 物流单号、节点、更新时间 | 注入 PrincipalContext，先验证订单归属 |
| create_ticket | order_id、issue_type、summary | ticket_id、status | 注入 PrincipalContext、action_id、确认凭证和幂等键 |

模型可见的工具 Schema 只包含业务参数。PrincipalContext、action_id、确认凭证和幂等键由安全包装层注入，模型不能查看或生成。模型也不能构造 URL、HTTP 方法、数据库连接或任意 Python 代码。

### 10.2 调用顺序

- 查询物流时先执行 query_order 验证订单归属，再执行 query_logistics。
- 查询结果显示异常时，先征求建单确认，再执行 create_ticket。
- 工具结果必须带 source、observed_at 和 status，答案不得把过期数据描述为当前状态。

### 10.3 重试策略

- 使用独立的连接、读取和总请求超时。
- 429 优先遵守 Retry After；未提供时使用指数退避加随机抖动。
- 网络错误、超时和 500、502、503、504 可重试；参数错误、401、403 和 404 不重试。
- 建议最多 3 次尝试，退避基线为 0.5 秒，且受单请求总截止时间限制。
- 对连续失败的下游启用断路器、并发隔离和恢复探测，避免请求堆积。
- create_ticket 只有在幂等键生效后才能自动重试。
- 连续失败时停止调用，说明当前无法核实，并提出建单选项；只有取得用户明确确认后才能创建工单。
- 观测、缓存等非关键依赖失败不能拖垮主链路。

## 11 API 与 SSE

### 11.1 接口

| 方法与路径 | 用途 | 关键行为 |
| --- | --- | --- |
| POST /chat | 对话入口 | 鉴权、参数校验、SSE 输出、断开取消 |
| GET /health | 存活与就绪检查 | 区分 liveness 与 readiness，检查数据库和向量库 |
| GET /metrics | Prometheus 指标 | 按环境限制访问，不暴露用户内容 |

### 11.2 SSE 事件

~~~text
event: step.started
data: {"step":"query_order","message":"正在查询订单"}

event: tool.completed
data: {"tool":"query_order","status":"success","summary":"已取得订单状态"}

event: action.preview
data: {"action_id":"act_abc123","action":"create_ticket","summary":"为订单 12345 创建退款异常工单","expires_at":"2026-09-23T15:30:00+08:00"}

event: message.delta
data: {"content":"订单 12345 当前处于退款处理中"}

event: done
data: {"trace_id":"tr_abc123","citations":[]}
~~~

事件类型固定为 step.started、tool.started、tool.completed、action.preview、warning、message.delta、done 和 error。事件可展示执行计划和工具结果摘要，但不得发送原始系统提示词、隐式推理链、访问令牌或未脱敏数据。

客户端断开后应取消仍在执行的只读查询和未提交工具调用。已经提交的 create_ticket 依赖幂等键查询最终状态，不能盲目重复调用。

每个事件都使用固定 Schema 和 JSON 序列化，禁止把未经处理的换行写入事件字段。响应设置 Cache Control no store，CORS 使用来源白名单；使用 Cookie 鉴权时同时配置 SameSite、Origin 校验和 CSRF 防护。

### 11.3 稳定错误码

API 对外返回稳定错误码和安全文案，不返回堆栈、文件路径或数据库异常原文。至少定义 MISSING_ARGUMENT、AUTHENTICATION_REQUIRED、FORBIDDEN_RESOURCE、SQL_REJECTED、SQL_EXECUTION_FAILED、RAG_NO_EVIDENCE、UPSTREAM_RATE_LIMIT、UPSTREAM_TIMEOUT、TICKET_CREATION_FAILED 和 INTERNAL_ERROR。

## 12 安全与数据治理

### 12.1 安全不变量

- 身份、授权、SQL 校验、工具权限、PII 脱敏和 SSE 输出过滤均由确定性代码执行，不能只依赖模型提示词。
- 用户输入、知识文档、数据库值、工具返回和对话摘要均是不可信数据，其中的文字不能提升权限、修改系统策略或直接触发写操作。
- 发生解析失败、授权不明、证据不足或安全组件异常时执行 fail closed，拒绝或降级，不扩大查询范围。
- 正则可以用于快速预筛，但不能作为 SQL 安全边界。最终判断必须基于 AST 和数据库运行时只读限制。

### 12.2 认证与对象级授权

1. /chat 在开始 SSE 前完成认证，服务端生成 principal_id、tenant_id 和 scopes。
2. 所有订单查询都强制加入 orders.user_id = :principal_id。聚合、分页、模糊商品查询和多表 Join 也不能省略。
3. 物流查询通过 logistics 关联 orders 后校验订单所有者。
4. 每个业务工具在自身边界再次校验对象归属，不能信任 Agent 已经检查。
5. 跨用户对象统一返回“未找到或无权访问”，避免通过差异响应枚举订单。
6. 个性化缓存键必须包含 tenant、principal、scope、规范化查询和数据版本。

### 12.3 Prompt 注入防护

- 检索 chunk 通过独立 evidence 字段传入模型，不拼接成系统指令。
- 系统提示明确声明检索文本和工具返回仅是数据，不具有指令优先级。
- 服务端策略决定当前可用工具；检索内容不能开启发券、建单、修改地址等权限。
- 对“忽略之前指令”等疑似注入内容进行标记和安全事件记录，但不把检测器当作唯一防线。
- 恶意文档即使未被检测到，也会因工具白名单、确认流程和授权校验而无法产生副作用。

### 12.4 PII 和密钥

- user_id、order_id、姓名、电话、地址和工单正文按敏感数据管理，查询与返回遵循最小必要原则。
- logs.jsonl 由生成器创建，只使用虚构身份和占位联系方式；CI 仍需扫描并阻止真实手机号、地址、令牌或其他高敏数据混入。
- 发送到公网模型前完成脱敏或令牌化；切换本地模型后仍保留相同规则。
- SSE、应用日志、Langfuse、错误和截图使用同一 redactor。禁止记录 Authorization、Cookie、API key、完整 SQL 参数和完整原始数据行。
- metrics label 禁止出现 user_id、order_id 等高基数或敏感值。
- .env 不进入 Git 或镜像。生产密钥通过只读 secret 挂载或密钥服务提供并支持轮换。

### 12.5 副作用工具

create_ticket 采用“预览、确认、执行”三步：

1. Agent 提议操作，策略层验证业务参数并生成 action_id。
2. 服务端将用户、会话、工具名、参数快照、参数哈希、过期时间和状态保存到独立的运行时状态库，再发送 action.preview。
3. 用户下一轮通过 /chat 的 action 字段提交 action_id 和 confirm 或 cancel，不重传工具参数。
4. 服务端校验用户、会话、参数哈希、有效期和一次性状态，确认后以 action_id 派生幂等键并执行。
5. 执行完成或取消后将 action_id 标记为已消费，重放请求只返回同一最终结果。

确认前、确认过期、参数变化、跨用户或重放都不得再次执行。模型看不到确认凭证。未来的修改地址、发券等工具沿用相同机制。

如果工单上游在用户确认后仍不可用，系统把请求写入独立于 ecommerce.db 的加密 Outbox，并返回 pending_ticket_id 和“待提交”状态；后台任务使用同一幂等键重试。只有上游确认成功后才能返回真实 ticket_id。Outbox 也不可写时应明确报告失败，不能声称已建单。

## 13 可观测与运行指标

### 13.1 Trace 结构

每个请求建立一个 trace，并包含以下 span：

1. authentication
2. route
3. retrieval 或 schema_linking
4. sql_generation 与 sql_validation
5. sql_execution 或 tool_call
6. answer_verification
7. response_stream

每个 span 记录开始时间、结束时间、状态、重试次数、token、错误类别和输入输出摘要。SQL span 记录脱敏后的 SQL 模板、执行耗时、返回行数和有限数据片段。

核心部署将 Trace、pending_action、幂等结果和 Outbox 保存到独立的 runtime/agent_state.db 或等价 TraceStore，不写入只读业务数据库。受保护的 /internal/traces/{trace_id} 提供只读查询和导出，用于完成 MVP 链路截图。Langfuse 作为可选异步 Exporter；未启用或导出失败时不影响核心 Trace 验收。

生产 Trace 需要 RBAC、加密、采样和留存期限。演示完整链路和截图使用合成用户与合成订单，不使用真实客服对话。

### 13.2 Prometheus 指标

- http_requests_total 与 http_request_duration_seconds
- agent_route_total
- rag_retrieval_duration_seconds 与 rag_no_evidence_total
- sql_validation_rejected_total
- sql_execution_duration_seconds 与 sql_correction_total
- tool_calls_total、tool_retry_total 与 tool_failure_total
- sse_connections_active
- llm_tokens_total
- ticket_created_total 与 ticket_idempotency_hit_total

/metrics 仅在内网开放或要求运维鉴权。/health 只返回组件状态和稳定错误码，不返回连接串、密钥或完整版本信息。

## 14 加分能力实施

### 14.1 缓存

- 公开政策回答使用 policy_key、知识库版本、模型版本和规范化问题组成缓存键。
- 个性化统计或订单结果必须包含 tenant、principal 和数据版本，禁止进入全局缓存。
- 政策重新入库后按 knowledge_version 主动失效。
- 实时订单、物流和异常结果使用短 TTL；权限失败不做跨用户负缓存。
- 缓存只保存净化后的结果，不保存访问令牌和工具确认状态。

### 14.2 图表生成

不执行模型生成的任意 Python 代码。实现固定 chart 工具，仅接受：

- chart_type：line 或 bar 等白名单类型。
- x、y：来自已授权 SQL 结果的字段。
- title、labels：长度受限的纯文本。

工具在无网络、只读文件系统和受限 CPU、内存、执行时间的环境中生成图片。图片使用随机对象 ID 返回，并继承原查询的访问权限和过期时间。

### 14.3 长会话压缩

- 保留最近若干轮原始消息，将更早对话压缩为结构化摘要。
- 订单号、待确认动作和用户偏好可进入摘要；principal_id、授权范围、确认状态等可信数据保存在独立状态中。
- 每次压缩后运行事实一致性检查，并记录摘要所覆盖的消息范围。
- 摘要不能覆盖系统策略，也不能直接授权工具。

### 14.4 本地模型与内网部署

ModelGateway 暴露统一 chat、structured_output、embedding 和 health 接口。公网模型和本地 vLLM 或 llama.cpp 通过以下配置切换：

~~~dotenv
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://vllm:8000/v1
LLM_MODEL=qwen3-8b
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL_PATH=/models/embedding
~~~

离线包包含镜像归档、Python wheelhouse、模型文件校验值、Qdrant 数据卷初始化脚本和安装手册。更换模型后必须重跑路由、结构化输出、SQL、工具和安全评测，不能沿用原模型结论。

## 15 配置与部署

### 15.1 Docker Compose 服务

| 服务 | 用途 | 持久化 |
| --- | --- | --- |
| api | FastAPI 与 Agent | 业务数据库只读；agent_state_data 保存 Trace、确认、幂等和 Outbox |
| qdrant | 向量检索 | qdrant_data |
| mock server | 项目自建的订单、物流、工单和故障注入服务 | 基于 data_manifest.json 初始化；独立保存 mock 工单 |
| redis | 缓存和可选会话检查点 | redis_data |
| langfuse | 可选 Trace 导出和展示 | 使用自托管依赖和独立数据卷 |
| local model | 离线推理，可选 profile | 模型目录只读挂载 |

仓库采用 docker 目录时，核心命令统一为 docker compose -f docker/docker-compose.yml up --build，并必须拉起 api、qdrant 和 mock server。本地 TraceStore 随 api 启动；Redis、Langfuse 和本地模型可使用 Compose profile，启用方式写入 README。

### 15.2 启动顺序

1. 校验 .env、密钥和 MOCK_RANDOM_SEED 是否完整。
2. 若数据资产不存在，则运行 generate_seed_data.py；若已存在，则使用 verify_data_manifest.py 核对文件哈希、记录数和跨文件引用。
3. 校验 ecommerce.db 可读、不可写，并核对 schema hash。
4. 校验 status 字典和 Schema Catalog 完整性。
5. 执行或核对知识入库 manifest。
6. 校验独立运行时状态库可写，并执行版本迁移。
7. 等待 Qdrant 与自建 mock server 就绪。
8. 启动 API，完成 readiness 检查后再接流量。

### 15.3 关键配置

~~~dotenv
APP_ENV=development
DATABASE_URI=file:/app/database/ecommerce.db?mode=ro
STATE_DB_URI=file:/app/runtime/agent_state.db
QDRANT_URL=http://qdrant:6333
MOCK_SERVER_URL=http://mock-server:8080
MOCK_RANDOM_SEED=20260923
MOCK_SCENARIO_CONTROL_ENABLED=false
SQL_MAX_ROWS=200
SQL_MAX_CORRECTIONS=2
TOOL_MAX_ATTEMPTS=3
REQUEST_DEADLINE_SECONDS=20
TRACE_ENABLED=true
TRACE_EXPORTER=none
PII_REDACTION_ENABLED=true
~~~

所有数值均可按压测结果调整。生产配置必须通过启动校验，不能在缺少授权、脱敏或只读设置时带病运行。

## 16 测试与评测

### 16.1 测试分层

| 层级 | 重点 |
| --- | --- |
| 单元测试 | 路由、槽位、版本裁决、SQL AST 规则、脱敏、重试计算、幂等键 |
| 集成测试 | Qdrant 检索、SQLite 只读执行、mock 服务错误、LangGraph 分支、SSE |
| 端到端测试 | 从 /chat 到答案、引用、Trace 和降级的完整流程 |
| 安全测试 | SQL 注入、间接 Prompt 注入、对象越权、PII、重复副作用 |
| 黑盒评测 | 真实问题分布下的答案、事实、工具和失败行为 |

pytest 用例数量不得少于题目要求的 8 条，实际应覆盖下表中的风险场景。

### 16.2 必测场景

1. 政策问题路由到 RAG，回答包含文档、段落和生效日期。
2. 五篇冲突旧政策不会覆盖最新有效版本；无法裁决时明确提示冲突。
3. 知识文档中的恶意指令不能改变路由、权限或工具集合。
4. 消费统计路由到 SQL，并与数据库聚合结果逐项一致。
5. 订单与物流 Join 正确，且只返回当前用户的数据。
6. 缺少订单号时进入澄清，工具调用次数为零。
7. SELECT 星号、多语句、注释逃逸、写语句、PRAGMA、ATTACH、sqlite_master 和危险函数全部被拒绝。
8. CTE、子查询和 UNION 的每个分支都递归通过安全检查。
9. 用户 A 猜测用户 B 的订单号、分页、聚合、缓存命中和工具直调均无法获取数据。
10. SQL 首次发生可修复错误后修正成功；超过次数后安全终止。
11. 429 遵守 Retry After，500 和超时按预算重试，达到上限后及时降级。
12. 未确认、伪造确认、过期确认和参数变化均不能创建工单。
13. action_id 的过期、跨用户、篡改和重放均被拒绝；SSE 重连或并发重复确认只生成一个工单。
14. 工单上游失败时只返回待提交状态，Outbox 可幂等恢复；Outbox 失败时不虚报成功。
15. SSE 事件顺序、JSON Schema、断线取消和最终 done 事件正确。
16. 响应、SSE、日志、Trace、metrics 和截图均不泄露密钥或高敏 PII。
17. 公网模型和本地模型仅改配置即可切换，且切换后重新通过结构化输出和安全测试。

SQL 安全测试需包含 Unicode 混淆、分号堆叠、注释、嵌套查询和资源耗尽样例。测试前后核对数据库文件哈希或关键表行数，证明数据库未发生写入。

### 16.3 Golden 评测集

从自行生成的 50 条 logs.jsonl 对话中筛选和扩展至少 30 条 golden 用例，按以下类型分层：

- RAG 与版本冲突
- SQL 聚合与 Join
- 订单及物流工具
- 缺参澄清
- 429、500 和超时
- 越权、SQL 注入和 Prompt 注入

每条用例包含 question、trusted_user、expected_route、required_tools、expected_arguments、expected_facts、expected_citations 和 forbidden_behaviors。全部对话使用合成身份，固定数据版本和随机种子，保证回归可复现。

### 16.4 指标与发布门槛

题目没有给出具体阈值，以下作为建议初始门槛，项目启动时确认：

| 指标 | 建议门槛 |
| --- | --- |
| Answer Relevancy | 不低于 0.85 |
| Faithfulness | 不低于 0.90 |
| Tool call Accuracy | 不低于 0.95 |
| 数值准确率 | 不低于 0.98 |
| 引用准确率 | 不低于 0.95 |
| SQL 可执行准确率 | 不低于 0.90 |
| 注入与越权拦截率 | 100% |
| 未确认写操作 | 0 |
| PII 或密钥泄露 | 0 |
| 同一幂等键重复建单 | 0 |

eval.py 输出 JSON 和 Markdown 报告，记录基线、改动、改后指标和失败样例。任何关键安全门禁失败时返回非零退出码并阻断发布。

## 17 实施阶段

| 阶段 | 主要工作 | 退出条件 |
| --- | --- | --- |
| 0 自建数据与接口基线 | 实现生成器，产出 SQLite、JSONL、知识库、状态字典、合成对话和 mock API | 固定 seed 可一键重建全部资产，manifest 与一致性检查通过 |
| 1 工程骨架 | 配置、ModelGateway、FastAPI、LangGraph State、CI | /health 可用，测试和静态检查可运行 |
| 2 RAG | 入库、去重、版本裁决、检索、引用、防注入 | 政策用例和冲突用例通过 |
| 3 Text to SQL | Schema Linking、生成、AST 校验、只读执行、自纠错 | 安全与准确性用例通过，数据库保持只读 |
| 4 工具编排 | 订单、物流、工单、澄清、确认、幂等、降级 | 多步调用和异常路径通过 |
| 5 服务与观测 | SSE、本地 TraceStore、可选 Langfuse Exporter、metrics、脱敏、缓存和限流 | 不依赖可选组件即可复现完整 Trace，敏感信息扫描通过 |
| 6 评测与交付 | Golden 回归、Compose、离线包、文档、录屏 | 验收矩阵全部有测试或证据 |

每个阶段合并前运行对应单元和集成测试，不将安全、评测和容器化留到最后一次集成。

## 18 MVP 验收矩阵

| 编号 | 能力 | 验收场景 | 通过证据 |
| --- | --- | --- | --- |
| 1 | 混合问答 | “退货政策”走 RAG；“上月消费金额”走 SQL | 路由事件和 Trace |
| 2 | RAG 问答 | 回答含文档名、段落、生效日期；冲突采用最新有效版并说明 | 自动测试与回答样例 |
| 3 | Text to SQL | 支持订单物流 Join；无 SELECT 星号；所有写 SQL 被拦截 | SQL 测试与只读证明 |
| 4 | 数据准确性 | 金额、数量、时间和状态与数据库结果一致 | 查询结果对账 |
| 5 | 工具调用 | 完成订单到物流链路；异常状态询问是否建单 | Tool Trace |
| 6 | 缺参澄清 | 未给订单号时不调用工具、不生成订单号 | 对话测试 |
| 7 | 防御与纠错 | 注入被拒绝；合法 SQL 错误可有限修正 | 对抗测试与重试 Trace |
| 8 | 兜底与降级 | 429、500、超时有界重试；失败后提示建单，用户确认后只创建一次并返回工单号；建单上游失败则进入 Outbox 待提交状态 | 双轮故障注入和幂等测试 |
| 9 | 流式输出 | 可见安全的步骤、工具摘要和答案增量，不泄露推理链 | SSE 协议测试 |
| 10 | 服务化 | /chat、/health、/metrics 可用；Compose 一键启动核心服务 | 启动记录与接口测试 |
| 11 | 可观测 | 核心本地 TraceStore 展示脱敏 prompt、路由、SQL 模板、耗时、工具链、结果摘要和 token | 受保护 Trace 页面或导出及合成数据截图 |

## 19 实施决定与剩余风险

题目所称随附资产均未提供。阶段 0 不再等待外部文件，而是构建、校验并固化全部 mock 数据和接口契约。原题对“不得修改所附 mock_server.py”的限制不再适用；自建 mock_server.py 是本项目受版本控制的源码。

### 19.1 数据建模决定

1. MVP 的 ecommerce.db 保留 orders、logistics、products 三张核心表，确保基础验收与题目描述一致。
2. “红色鞋子到商品再到订单”需要订单商品关系。仅在实现该加分项时增加 order_items(order_id, sku_id, quantity, unit_price)，并在 README 标明这是扩展表。
3. 退款进度由 mock API 的实时订单状态提供；SQLite 保存可用于分析的状态快照。未建 refunds 表前不回答题目数据中不存在的退款金额。
4. status 数值映射、异常状态和迁移规则统一写入 schema_catalog.yaml，由生成器、mock 服务和 Agent 共同使用。
5. orders.jsonl 是原始样例，有效记录进入 SQLite，异常记录保留为负向测试；分页仅由 mock API 契约定义。
6. 金额币种固定为 CNY，使用两位小数；相对日期按 Asia/Shanghai 解释。

### 19.2 接口与权限决定

1. SQLite 是历史统计权威源，自建 mock API 是实时订单、物流和工单权威源；两者的共有字段必须通过 manifest 一致性检查。
2. create_ticket 的请求、响应、幂等和故障语义由本项目定义，并使用独立 Outbox 支持待提交状态。
3. 开发和测试环境使用固定的合成 Bearer Token 映射 PrincipalContext，至少提供用户 A、用户 B 和管理员三种身份；生产认证通过适配器替换。
4. 修改地址不属于 MVP，不在 mock 服务中实现写接口。
5. mock 服务的故障注入开关只在测试环境开放，且不接受来自最终用户消息的场景选择。

### 19.3 自建数据与模型风险

1. 自建数据只能证明系统闭环和防御逻辑，不能代表真实电商数据分布。报告需明确其合成属性，不夸大线上效果。
2. 生成器可能导致评测泄漏。训练或调试样例与 held-out golden 使用不同随机种子和模板，禁止把 expected answer 写入 Prompt。
3. 知识文档统一使用 ISO 日期；缺失 effective_date 的文档拒绝入库，未来生效文档在生效前不参与回答，同日冲突按显式 version 再裁决。
4. policy_key、段落 ID 和引用格式由生成器稳定生成，修改规则时必须提升 knowledge_version 并重建索引。
5. Embedding 模型、chunk 大小、召回数量和重排方式仍需通过 held-out 评测确定。
6. 本地模型目标硬件、量化方式、上下文长度、并发和 QPS 未定义，资源报价只能在压测后给出。

### 19.4 运行与验收

1. 业务延迟、并发和 QPS 没有硬性指标，应在目标环境压测后确定 SLO。
2. 图表文件的存储服务、授权、过期和清理规则未定义。
3. 会话记忆是仅限当前会话还是跨会话保存，以及保存和删除周期尚未明确。
4. 离线目标操作系统、CPU 架构、CUDA 版本和镜像交付介质需提前确认。
5. 录屏的时长、格式、演示场景和提交位置需在交付前确定。

## 20 交付检查清单

- [ ] agent 目录包含 graph、tools、retriever、sql 和 guards。
- [ ] api 目录包含 /chat、/health、/metrics。
- [ ] eval 目录包含不少于 30 条 golden、eval.py 和报告。
- [ ] knowledge 目录包含约 30 篇生成文档、入库脚本和 manifest，其中至少 5 篇构成可验证的版本冲突。
- [ ] database 目录包含 schema、200 条 orders.jsonl、生成后的 ecommerce.db、异常记录报告和 Schema Catalog。
- [ ] scripts 目录可用固定 seed 重建全部数据资产，并通过 data_manifest.json 校验。
- [ ] mock 目录包含自建 mock_server.py、接口说明和 429、超时、500、分页、工单幂等场景。
- [ ] docker 目录包含 Dockerfile、Compose 和离线部署配置。
- [ ] docs 目录包含部署手册、架构设计、交付清单、FAQ、已知问题与风险；完成加分交付时另含资源与报价估算。
- [ ] README 为中文，写明启动、初始化、测试、评测、查看 Trace、模型切换、架构图、选型理由、指标表和已知问题。
- [ ] 依赖版本已锁定，.dockerignore、.env.example 和 .pre-commit-config.yaml 完整；Black 与 Ruff 已配置，mypy 可按项目成熟度启用，密钥未入库。
- [ ] 50 条 logs.jsonl 合成对话不含真实 PII，held-out golden 与调试数据使用不同 seed。
- [ ] 至少一条完整链路截图使用合成数据且已脱敏。
- [ ] 录屏覆盖政策问答、数据查询、缺参澄清、工具链、失败降级和 Trace。
- [ ] 所有 MVP 验收项均有自动化测试、运行记录或截图证据。

## 21 完成定义

只有同时满足以下条件才可标记为完成：

1. 在没有任何随题数据的干净环境中，可以用固定 seed 生成全部资产并由 Docker Compose 启动核心服务。
2. 11 项 MVP 验收项全部通过。
3. 数据库保持只读，注入、越权、未确认写入和 PII 泄露测试零失败。
4. Golden 评测达到确认后的门槛，并保留可复现报告。
5. 公网与本地模型通过配置切换，不修改业务代码。
6. Trace、指标、日志和故障降级可在演示环境复现。
7. README、部署手册、FAQ、风险清单和录屏齐全。

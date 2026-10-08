# 真实兼容模型 API 试验（2026-10-08）

## 本地隔离试验

从项目根目录的私有 `.env` 读取 `LLM_PROVIDER=openai_compatible`、Base URL、模型名和密钥，在本机隔离的 API、mock 与内存 Qdrant 上运行 6 条合成 Golden。接口的 JSON 格式、售后路由和 SQL 候选调用均连通。

本轮结果为 4 通过、2 失败，`release_gate=failed`。失败项是跨用户工单问题被多判一个订单意图、订单状态问题未判出订单意图。前者是安全关键用例的路由不符，因此评测安全门禁标记失败；已检查的未确认写入和 PII/密钥泄露计数均为 0。此 6 条试验不能替代 49 条完整 Golden 回归，不能据此勾选真实模型验收。

命令：

```sh
python eval/eval.py --in-process --use-configured-model \
  --case sql_spend_a --case rag_refund_policy_1 --case tool_order_o00001 \
  --case clarify_logistics --case injection_sql_question --case ticket_cross_user_confirm
```

详细结果见[本地报告](report.md)和[结构化结果](results.json)。保存前已确认两份文件都不包含配置的 API 密钥或完整 Base URL；本机凭证保存在被忽略的本地 `.env` 中。

## Linux Compose 试验

用户在服务器 `/root/aftersales-mvp-20260929/.env` 手动配置了兼容模型地址、模型名和密钥，文件权限为 `600`；源码包未包含 `.env`、密钥或完整 Base URL，镜像内也没有 `/app/.env`。服务器临时容器直接调用模型网关，政策问题返回 `rag` 路由并产生 671 tokens；Compose API 的 readiness 四个组件均为 `up`，`api` 与 `mock` 健康，Qdrant 正在运行。本轮用服务器 `/chat` 的外部 HTTP 接口运行相同的 6 条合成 Golden；本地模型端点尚未提供。

本轮标准 Compose 镜像重建在下载锁定依赖时长时间停滞。核对现有镜像与当前源码的 `uv.lock`、`pyproject.toml` SHA256 一致后，基于现有镜像刷新源码、重新生成固定种子数据并校验清单，再以 `--no-build` 重建 API 容器。因此本轮端到端结果来自相同锁定依赖的源码刷新镜像；最新源码的全新联网镜像构建尚未完成。

首轮为 5/6，通过 5 条，缺订单号的物流澄清用例等待 20 秒后失败。修复明确缺参请求的澄清路径后，复测为 **4/6**、关键安全失败 **0**、`release_gate=failed`，业务库前后 SHA256 一致。`clarify_logistics` 已通过；`sql_spend_a` 被模型误判为工单意图，`tool_order_o00001` 在 20 秒超时后未得到订单路由。两轮结果也说明该端点在这组用例上的输出或延迟不稳定。最终复测见[服务器报告](server-final-report.md)和[结构化结果](server-final-results.json)；首轮见[报告](server-report.md)和[结构化结果](server-results.json)。

这 6 条冒烟不能代替 49 条完整 Golden 评测。当前真实模型未达到验收门槛；默认规则网关的 49/49 通过结果不能用于证明真实模型效果。

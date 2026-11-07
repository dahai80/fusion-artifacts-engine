---
doc_id: fusion-artifacts-engine/config-reference
product: fusion-artifacts-engine
class: app
entry: service
doc_type: config-reference
title: { zh: "配置参考", en: "Configuration Reference" }
audience: engineer
status: draft
version: 0.7.13
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - config.example.yaml
  - fusion_artifacts_engine/config.py
---

# 配置参考 / Configuration Reference

配置为 YAML(`config.example.yaml` 为带注释模板,`fusion_artifacts_engine/default_config.yaml` 为内置默认),由 `fusion_artifacts_engine/config.py` 加载;关键项可用环境变量覆盖。

## server

| 键 | 说明 |
|---|---|
| `host` | 监听地址,默认 `127.0.0.1`;环境变量 `FUSION_ARTIFACTS_HOST` 覆盖 |
| 端口 | 默认 11451;CLI `--port` 或 `FUSION_ARTIFACTS_PORT` 覆盖 |

## storage / thresholds / artifact

存储根目录与压缩阈值、产物默认策略(模板与注释见 `config.example.yaml` 对应段)。

## security

| 键 | 说明 |
|---|---|
| `allow_no_auth` | 默认 `false`(fail-closed);`true` 跳过 `X-API-Key` 校验,仅限开发环境 |
| API key | 密钥配置项;请求以 `X-API-Key` 头携带 |

## rate_limit

| 键 | 说明 |
|---|---|
| `rps` | JSON-RPC + 认证 REST 的令牌桶速率(默认 200) |
| `public_rps` | 未认证公开分享路径的独立令牌桶(默认 50) |

## metrics / tracing

| 键 | 说明 |
|---|---|
| `metrics.token` | 可选;设置后 `/metrics` 要求匹配的 `X-Metrics-Token` 头 |
| tracing | OTel 为可选 extra(`pip install -e ".[otel]"`),local-first 单租户默认关闭 |

## sse / gateway

SSE 推送参数与上游 fusion-gateway 连接配置(`fusion_artifacts_engine/gateway_client.py` 消费)。

约束:改配置后用 `fusion-artifacts-engine status` + 一次真实 JSON-RPC 调用验证;`allow_no_auth: true` 永远不应出现在生产配置中。

---
doc_id: fusion-artifacts-engine/troubleshooting
product: fusion-artifacts-engine
class: app
entry: service
doc_type: troubleshooting
title: { zh: "故障排查", en: "Troubleshooting" }
audience: engineer
status: draft
version: 0.7.13
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - start.sh
---

# 故障排查 / Troubleshooting

先跑 `fusion-artifacts-engine status`(exit 0 = 健康,1 = 未运行),再看 `logs/` 与 `GET /metrics`。

## 启动

**`start` 后端口未监听**

1. `FUSION_ARTIFACTS_PORT` / CLI `--port` 冲突:`lsof -i :11451` 看占用;start.sh 有端口注册表自检(架构仓 `architecture/port-registry.yaml`),未注册端口会拒绝启动。
2. 配置 YAML 语法错误:`config.py` 加载失败会直接退出,用 `python3 -c "import yaml,sys; yaml.safe_load(open('config.yaml'))"` 先验证。

**Docker 容器数据丢失**

镜像持久化路径是 `/data` 卷;确认 `-v` 挂载指向宿主目录,否则容器重建后产物清空。

## 认证与限流

**所有请求 401/403**

- 认证 fail-closed:除公开分享路径外都要 `X-API-Key` 头;确认密钥与 `security` 段一致。
- `allow_no_auth: true` 只是显式跳过认证,出现该状态说明配置处于开发模式,生产必须关闭。

**429 Too Many Requests**

两个独立令牌桶:认证请求 `rate_limit.rps`(默认 200)、公开分享 `rate_limit.public_rps`(默认 50)。触发的是哪个桶看请求是否带密钥;等待窗口或调阈值。

## 运行期

**调用 `artifact.inject` / `artifact.interact` 返回 -32005**

这两个方法已退役,方法名保留只为旧客户端兼容;新代码改用 `artifact.get_content` 取回内容。

**`-32601 method not found`**

方法名拼写或协议版本问题;以 `fusion_artifacts_engine/rpc/methods.py` 的注册表为准。

**上下文预算异常膨胀**

`context.budget` 检查会话引用是否泄漏全量内容;确认客户端持有的是轻量引用,必要时 `artifact.auto_compact` 触发压缩。

**AI 生成失败**

`ai.generate` 依赖 fusion-gateway 连接(见 `gateway` 配置段);先确认网关在线,`ai.list_models` 验证可达性。

## 可观测性

- `GET /metrics`:Prometheus 指标;若配置了 `metrics.token`,请求需带 `X-API-Token` 头。
- OTel 追踪默认关闭,安装 `.[otel]` extra 后按 `tracing` 配置段启用。
- 测试基线:`pytest tests/`。

仍未解决:附 `fusion-artifacts-engine status` 输出、`logs/` 对应时间段片段与最小复现 JSON-RPC 请求提 issue。

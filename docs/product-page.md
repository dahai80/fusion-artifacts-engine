---
doc_id: fusion-artifacts-engine/product-page
product: fusion-artifacts-engine
class: app
entry: service
doc_type: product-page
title: { zh: "产品页", en: "Product Page" }
audience: engineer
status: draft
version: 0.7.13
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
---

# fusion-artifacts-engine

Fusion 架构的结构化产物 CRUD 中间件。把模型生成的长内容(代码、文档、HTML 应用)从聊天消息中物理分离,解决长对话的上下文溢出。

## 它解决什么问题

| 上游过去要自己做的 | fusion-artifacts-engine 接管后 |
|---|---|
| 长内容整段塞进对话历史,几轮就撑爆窗口 | 全量内容独立存储,对话里只留约 30 token 的轻量引用,模型需要时按需取回(`artifact.get_content`) |
| 自己实现产物版本管理 | 版本列表、diff、快照、回滚开箱即用(`artifact.version_list` / `version_diff` / `version_rollback` / `create_snapshot`) |
| 自行搭建分享与渲染 | 一次生成分享链接(`artifact.create_share` / `get_shared` / `revoke_share`),Markdown/HTML 渲染(`artifact.render` / `render.visual`) |
| 手工做长内容预算控制 | `context.budget` 统一计算上下文预算;`auto_compact` 自动压缩 |

效果基线(来自 README):10 轮 1000 行代码迭代约 15k token,而不是约 120k。

## 核心能力与边界

- **JSON-RPC 服务**(主入口):标准 HTTP 端口 11451,stdlib 实现(无 FastAPI/uvicorn 依赖),方法族 `artifact.*` / `folder.*` / `ai.*` / `context.budget` / `render.visual`。约束:`artifact.inject` / `artifact.interact` 已退役,方法名保留仅为旧客户端兼容,调用返回 not-implemented。
- **安全默认 fail-closed**:`X-API-Key` 认证,`security.allow_no_auth: false` 为默认;公开分享走独立限流桶(`rate_limit.public_rps`)。
- **生命周期**:回收站(`list_recycle` / `restore` / `purge_expired`)、过期清理、star/pin。
- **可观测性**:`/metrics`(可选 `X-API-Token` 保护)、OTel 追踪为可选 extra(`.[otel]`,local-first 单租户默认不需要)。
- **容器化**:自带 Dockerfile,数据持久化到 `/data` 卷,镜像与本地同一 stdlib 服务器。
- 约束(通用):单机中间件,不做跨节点同步;webui/ 为辅助界面,服务契约以 JSON-RPC 为准。

## 入口

- 守护进程:`fusion-artifacts-engine start --port 11451`;状态:`fusion-artifacts-engine status`(exit 0 健康)。
- 文档:[get-started.md](get-started.md) · [api-reference.md](api-reference.md) · [config-reference.md](config-reference.md) · [troubleshooting.md](troubleshooting.md) · 场景化 [USAGE_GUIDE.md](../docs/USAGE_GUIDE.md)。

## 适用与不适用

- 适用:AI 对话产物托管、长上下文预算治理、产物版本与分享。
- 不适用:通用对象存储;多租户云端服务。

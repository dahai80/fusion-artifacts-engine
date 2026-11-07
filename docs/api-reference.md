---
doc_id: fusion-artifacts-engine/api-reference
product: fusion-artifacts-engine
class: app
entry: service
doc_type: api-reference
title: { zh: "API 参考", en: "API Reference" }
audience: engineer
status: draft
version: 0.7.13
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - fusion_artifacts_engine/rpc/methods.py
  - fusion_artifacts_engine/rpc/errors.py
---

# API 参考 / API Reference

传输:JSON-RPC 2.0 over HTTP(端口 11451,stdlib 服务器)。认证:除公开分享路径外全部要求 `X-API-Key`。错误码沿用 JSON-RPC 标准(-32700 parse / -32600 invalid request / -32601 method not found / -32602 invalid params),业务扩展码 -32000~-32099(如 -32005 not-implemented,不可重试)。

## 方法族(定义在 `fusion_artifacts_engine/rpc/methods.py`)

### CRUD 与读取

| 方法 | 说明 |
|---|---|
| `artifact.create` / `artifact.create_external` | 创建产物(外部内容源) |
| `artifact.get` / `artifact.load` / `artifact.list` / `artifact.list_all` | 读取与列表 |
| `artifact.get_content` | 按需取回全量内容(对话内只存轻量引用) |
| `artifact.update` / `artifact.patch` / `artifact.rename` | 更新 |
| `artifact.delete` / `artifact.duplicate` / `artifact.remix` | 删除 / 复制 / 派生 |
| `artifact.view` / `artifact.watch` / `artifact.sync` | 浏览计数 / 关注 / 同步 |

### 版本、快照与回收站

| 方法 | 说明 |
|---|---|
| `artifact.version_list` / `artifact.version_diff` / `artifact.version_rollback` | 版本列表 / 差异 / 回滚 |
| `artifact.create_snapshot` / `artifact.list_snapshots` | 快照 |
| `artifact.list_recycle` / `artifact.restore` / `artifact.purge_expired` | 回收站与过期清理 |

### 组织与标记

| 方法 | 说明 |
|---|---|
| `artifact.star` / `artifact.pin` | 星标 / 置顶 |
| `artifact.add_tag` / `artifact.remove_tag` / `artifact.list_tags` / `artifact.list_artifact_tags` | 标签 |
| `artifact.create_folder` / `artifact.list_folders` / `artifact.move_to_folder` / `artifact.rename_folder` / `artifact.delete_folder` | 文件夹 |
| `artifact.list_by_source` / `artifact.move_to_project_kb` | 按源查询 / 移入项目知识库 |
| `artifact.comment` / `artifact.list_comments` | 评论 |
| `artifact.toc` / `artifact.export` / `artifact.import` / `artifact.export_code` / `artifact.import_code` / `artifact.export_session` | 目录 / 导入导出 |

### 分享、渲染、压缩、AI 与事件

| 方法 | 说明 |
|---|---|
| `artifact.create_share` / `artifact.get_shared` / `artifact.revoke_share` | 分享链接(公开路径,独立限流) |
| `artifact.render` / `render.visual` | Markdown/HTML 渲染 / 视觉原语渲染 |
| `artifact.check_safety` / `artifact.auto_compact` / `artifact.compacted` | 安全检查 / 自动压缩 |
| `context.budget` | 会话上下文预算 |
| `ai.generate` / `ai.list_models` | AI 生成与模型清单(经 gateway) |
| `artifact.emit_event` / `artifact.list_events` / `artifact.list_comments` | 事件流 |
| `artifact.workflow.create` / `artifact.workflow.revision` | 工作流产物 |

### 已退役(兼容保留)

`artifact.inject`、`artifact.interact` —— 方法名仍在注册表中(旧客户端不会收到 -32601),调用返回 -32005 not-implemented;新代码禁止使用。

## 事件与指标

产物生命周期事件(create/update/shared/moved/purged 等)经 `rpc/event_bus.py` 广播,SSE 推送配置见 `sse` 配置段;`GET /metrics` 输出 Prometheus 指标(可选 `X-Metrics-Token`),`rpc.server.duration` 为 RPC 耗时直方图。

限制:参数 schema 以 `methods.py` 为准,本文件只列方法与语义;新增方法须同步本表。

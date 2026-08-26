# Fusion Artifacts Engine

**[English](./README.md)** | 中文

Fusion 架构的结构化产物 CRUD 中间件。将生成产物与聊天消息物理分离，解决长 AI 对话中的上下文溢出问题。

> **新增：[使用指南 / Usage Guide](./docs/USAGE_GUIDE.md)** — 中英双语，场景式引导，含可运行示例（创建/版本/分享/渲染/预算/生命周期/回收站/运维）。

## 工作原理

当模型生成长内容（代码、文档、HTML 应用）时，引擎：
1. 独立存储完整内容
2. 在对话中用轻量引用替换（约 30 tokens）
3. 仅在模型需要时按需返回内容

效果：**10 轮 1000 行代码迭代仅使用约 15k tokens，而非约 120k**。

## 快速开始

```bash
# 安装
pip install -e ".[all]"

# 启动守护进程
fusion-artifacts-engine start --port 11451

# 查看状态
fusion-artifacts-engine status
```

## 认证

引擎通过 `X-API-Key` 请求头进行 API Key 认证。

- 若配置了 `api_key`，所有请求必须包含 `X-API-Key: <key>`
- 若**未**配置 `api_key`，默认**拒绝**所有请求（fail-closed，`allow_no_auth: false`）
- 生产环境：设置 `api_key`（env `FUSION_ARTIFACTS_API_KEY` 或 `security.api_key`）强制认证
- 本地单机受信网络：可显式 `allow_no_auth: true` 跳过鉴权（不推荐公网/多租户）

```yaml
# default_config.yaml
security:
  allow_no_auth: false
  recycle_retention_days: 7
```

## JSON-RPC API

引擎暴露 HTTP JSON-RPC 2.0 服务。示例：

```bash
curl -X POST http://127.0.0.1:11451 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"artifact.create","params":{"session_id":"sess_1","name":"hello.py","type":"code","content":"print(\"hello\")"},"id":1}'
```

### 核心方法

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.create` | session_id, name, type, content, summary?, change_log?, kind?, project_id?, metadata? | 创建产物 + v1 |
| `artifact.get` | artifact_id, project_id? | 获取元数据 |
| `artifact.get_content` | artifact_id, version? | 获取版本内容 |
| `artifact.list` | session_id, include_deleted?, project_id?, metadata_filter? | 列出会话产物 |
| `artifact.delete` | artifact_id, soft_delete?, project_id? | 软/硬删除产物 |
| `artifact.update` | artifact_id, content, change_log?, source?, expected_content_hash? | 创建新版本（乐观锁） |
| `artifact.patch` | artifact_id, operation, anchor?, content?, expected_version? | 补丁更新（replace_section/append/prepend/delete_section） |
| `artifact.load` | artifact_id, preview_only?, section? | 加载产物（预览/章节/全文，含章节token数） |
| `context.budget` | session_id, context_window? | 会话令牌预算汇总 |
| `artifact.auto_compact` | artifact_id, token_budget | 自动压缩产物至令牌预算内 |
| `artifact.version_diff` | artifact_id, from_version, to_version | 两版本间统一差异对比 |
| `artifact.render` | content, session_id, lang_hint, project_id? | 自动检测类型，创建可渲染产物 |
| `artifact.check_safety` | messages, output_budget | 令牌预算安全检查 |
| `artifact.inject` | messages, output_budget | **已下线 (运维6)**：抛 `-32005` NotImplementedError；请用 `context.budget` + `check_safety` |
| `artifact.interact` | artifact_id, action, payload, session_id? | **已下线 (运维6)**：抛 `-32005` NotImplementedError；不支持动作分发 |
| `artifact.sync` | artifact_id, file_path, direction | 产物内容与文件双向同步 |
| `artifact.version_list` | artifact_id | 列出所有版本 |
| `artifact.version_rollback` | artifact_id, target_version | 回滚到指定版本 |
| `artifact.export` | artifact_id, include_versions? | 导出产物数据 |
| `artifact.export_session` | session_id, output_dir | 批量导出会话 |
| `artifact.import` | session_id, data | 导入产物 |
| `artifact.export_code` | artifact_id, language? | 导出为源代码 |
| `artifact.import_code` | session_id, code, language?, name?, metadata? | 从代码创建产物 |
| `artifact.watch` | artifact_id, action, watcher_id?, since_version? | 监听变更 |
| `ping` | — | 健康检查 |

### 生命周期方法 (P1)

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.rename` | artifact_id, name | 重命名产物 |
| `artifact.star` | artifact_id, starred | 收藏/取消收藏 |
| `artifact.pin` | artifact_id, pinned, chat_id? | 固定/取消固定到聊天 |
| `artifact.duplicate` | artifact_id | 复制产物（新 ID） |
| `artifact.list_all` | filters?, sort?, page?, page_size?, cursor? | 列出所有产物（跨会话）；响应含 `next_cursor` 用于游标分页（仅 `updated_at`/`created_at` 排序） |

### 回收站方法 (P1)

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.list_recycle` | — | 列出已软删除的产物 |
| `artifact.restore` | artifact_id | 从回收站恢复 |
| `artifact.purge_expired` | — | 硬删除过期的回收项 |

### 分享方法 (P1)

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.create_share` | artifact_id, max_accesses?, expires_at? | 创建分享链接 |
| `artifact.get_shared` | share_id | 获取分享的产物（公开） |
| `artifact.revoke_share` | share_id | 撤销分享链接 |

### 快照方法 (P2)

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.create_snapshot` | artifact_id, label? | 为当前内容创建命名快照 |
| `artifact.list_snapshots` | artifact_id | 列出快照 |

### 文件夹方法 (P2)

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.create_folder` | name, parent_id? | 创建文件夹 |
| `artifact.list_folders` | — | 列出所有文件夹 |
| `artifact.rename_folder` | folder_id, name | 重命名文件夹 |
| `artifact.delete_folder` | folder_id | 删除文件夹 |
| `artifact.move_to_folder` | artifact_id, folder_id | 移动产物到文件夹 |

### 标签方法 (P4)

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.add_tag` | artifact_id, tag_name | 添加标签（不存在则创建） |
| `artifact.remove_tag` | artifact_id, tag_name | 移除标签 |
| `artifact.list_tags` | — | 列出所有标签 |
| `artifact.list_artifact_tags` | artifact_id | 列出产物的标签 |

### 事件方法 (P4)

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.emit_event` | artifact_id, event_type, payload? | 发射事件 |
| `artifact.list_events` | artifact_id?, session_id?, since_ts?, page?, page_size? | 列出事件 |

### 项目知识库方法 (P3)

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.move_to_project_kb` | artifact_id, project_id | 移动产物到项目知识库 |

### 外部模块方法

| 方法 | 参数 | 说明 |
|---|---|---|
| `artifact.create_external` | source_module, workspace_id, name, type, content, workflow_run_id?, summary?, kind?, project_id?, metadata? | 从外部模块创建产物（如 fusion-mlx） |
| `artifact.list_by_source` | source_module, workspace_id?, workflow_run_id? | 按来源模块列出产物 |

### 产物分类（Kind）

语义分类，匹配用户对产物的认知方式：

| Kind | 说明 |
|---|---|
| `app` | 交互式 Web 应用、仪表盘、网站 |
| `code` | 代码片段、算法、脚本 |
| `document` | 结构化文档、报告、模板 |
| `game` | 可玩游戏、模拟、交互挑战 |
| `tool` | 生产力工具、计算器、规划器 |
| `template` | 创意项目、测验、可复用模式 |

未指定 `kind` 时，根据 `type` 自动推断：
- `html` / `react` → `app`
- `markdown` → `document`
- `code` → `code`
- `data` → `tool`

### 版本来源追踪

每个版本记录其 `source`：
- `manual` — 用户手动编辑
- `ai_generation` — AI 驱动更新

省略 `change_log` 时，从内容差异自动生成（如 "+5 行, -2 行"）。

### 乐观锁

`artifact.update` 方法支持可选的 `expected_content_hash` 参数。提供时，若当前内容哈希不匹配则更新失败，防止并发场景下的更新丢失。

```bash
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.update","params":{"artifact_id":"art_xxx","content":"...","expected_content_hash":"sha256:abc123"}}'
```

### 代码-产物同步

**导出代码** — 获取产物内容作为源代码：
```bash
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.export_code","params":{"artifact_id":"art_xxx","language":"python"}}'
```

**导入代码** — 从代码创建产物：
```bash
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.import_code","params":{"session_id":"s1","code":"def foo(): pass","language":"python"}}'
```

**监听** — 轮询自某版本以来的变更：
```bash
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.watch","params":{"artifact_id":"art_xxx","action":"poll","since_version":3}}'
```

### 项目作用域

产物可按 `project_id` 限定作用域，实现多项目隔离：

```bash
# 创建时指定项目
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.create","params":{"session_id":"s1","name":"app.py","type":"code","content":"...","project_id":"my-project"}}'

# 列出项目内的产物
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.list","params":{"session_id":"s1","project_id":"my-project"}}'
```

### 元数据

产物支持任意 JSON 元数据，用于过滤和分类：

```bash
# 创建时附带元数据
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.create","params":{"session_id":"s1","name":"Button.tsx","type":"react","content":"...","metadata":{"framework":"react","component_name":"Button"}}}'

# 按元数据过滤列出
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.list","params":{"session_id":"s1","metadata_filter":{"framework":"react"}}}'
```

元数据以 JSON 格式存储在 SQLite 中，通过 `json_extract()` 查询。可将 `metadata_filter` 与 `project_id` 组合进行作用域查询。

## REST API

### REST /api/v1 (P3)

与 JSON-RPC 并行的 RESTful CRUD API：

**GET** 端点：
```bash
GET /api/v1/artifacts          # 列出产物（支持查询参数：created_by, since, until, kind, type, sort, page, page_size）
GET /api/v1/external?source_module=fusion-mlx&workspace_id=ws-001  # 按来源模块列出产物
GET /api/v1/folders            # 列出文件夹
GET /api/v1/tags               # 列出标签
GET /api/v1/events             # 列出事件
GET /api/v1/recycle            # 列出回收站
GET /api/v1/share/{share_id}   # 公开分享访问（免鉴权；已撤销/过期返回 410 Gone）
```

`GET /api/v1/artifacts` 查询参数：
- `created_by` — 按所有者过滤
- `since` / `until` — 按创建时间过滤（Unix 时间戳）
- `kind` — 按分类过滤（app/code/document/game/tool/template）
- `type` — 按类型过滤（code/markdown/html/react/data）
- `sort` — 排序字段（updated_at, created_at, name, starred）
- `page` / `page_size` — 分页

**POST** 端点（基于动作）：
```bash
POST /api/v1/rename            # {"artifact_id": "...", "name": "..."}
POST /api/v1/star              # {"artifact_id": "...", "starred": true}
POST /api/v1/pin               # {"artifact_id": "...", "pinned": true}
POST /api/v1/duplicate         # {"artifact_id": "..."}
POST /api/v1/restore           # {"artifact_id": "..."}
POST /api/v1/move-to-kb        # {"artifact_id": "..."}
POST /api/v1/move-to-folder    # {"artifact_id": "...", "folder_id": "..."}
POST /api/v1/snapshot          # {"artifact_id": "...", "label": "..."}
POST /api/v1/share             # {"artifact_id": "...", "max_accesses": 10}
POST /api/v1/tags              # {"artifact_id": "...", "tag_name": "..."}
POST /api/v1/folders           # {"name": "...", "parent_id": "..."}
POST /api/v1/events            # {"artifact_id": "...", "event_type": "..."}
POST /api/v1/purge             # {}
POST /api/v1/external/create   # {"source_module": "fusion-mlx", "workspace_id": "ws-001", "name": "...", "type": "code", "content": "..."}
```

### SSE 事件流 (P4)

Server-Sent Events 端点，实时推送产物变更通知：
```bash
curl -N http://127.0.0.1:11451/api/v1/events/stream
```
返回 `text/event-stream`，每 30 秒发送心跳（可通过 `sse.heartbeat_interval` 配置）。

事件通过内部 EventBus 实时推送——无需轮询。支持的事件类型：`artifact.created`、`artifact.updated`、`artifact.deleted`。

按产物分类过滤：
```bash
curl -N "http://127.0.0.1:11451/api/v1/events/stream?kind=app"
```

## Python SDK

```python
import asyncio
from fusion_artifacts_engine import ArtifactEngine, ArtifactEngineConfig

engine = ArtifactEngine()


async def main():
    # 创建并指定分类
    art, ver, ref = await engine.create_artifact(
        "sess_1",
        "app.py",
        "code",
        "print('hello')\n" * 50,
        summary="主应用",
        kind="tool",
    )
    print(f"已创建: {art.id} kind={art.kind}")

    # 更新并追踪来源
    v2, ref2 = await engine.create_version(
        art.id, "print('world')\n" * 60, change_log="", source="ai_generation"
    )

    # 导出为代码
    code_data = engine.export_code(art.id, "python")

    # 从代码导入
    new_art, _, _ = await engine.import_code(
        "s1", "def bar(): pass", language="python", name="bar.py"
    )

    # 生命周期操作
    await engine.rename_artifact(art.id, "new_name.py")
    await engine.star_artifact(art.id, True)
    await engine.duplicate_artifact(art.id)

    # 分享
    share = await engine.create_share(art.id, max_accesses=10)

    # 文件夹与标签
    folder = await engine.create_folder("我的文件夹")
    await engine.move_to_folder(art.id, folder.id)
    await engine.add_tag(art.id, "重要")

    engine.close()


asyncio.run(main())
```

## 支持的产物类型

| 类型 | 说明 |
|---|---|
| `code` | Python、JS、Rust 等 |
| `markdown` | 文档、README |
| `html` | 网页、仪表盘 |
| `react` | React/JSX 组件 |
| `data` | JSON、CSV、YAML |

## 存储

- **元数据**：SQLite，位于 `~/.fusion/artifacts/meta.db`
- **内容**：`~/.fusion/artifacts/content/{art_id}/v{num}.{ext}`
- 小内容（<10KB）内联存储在 SQLite 中
- 大内容存储在文件系统

### 数据模型

**Artifact** — 核心实体，包含所有权、生命周期和组织字段：
- 所有权：`owner_user_id`、`ownership_type`（personal/team/project）
- 生命周期：`is_deleted`、`deleted_at`（软删除）、`is_starred`、`is_pinned`、`pinned_chat_id`
- 组织：`folder_id`、`share_id`、`in_project_kb`、`content_hash`、`active_in_session`
- 外部来源：`source_module`、`workspace_id`、`workflow_run_id`

**ArtifactVersion** — 版本化内容，支持快照：
- 快照：`snapshot_type`（auto/named）、`snapshot_label`、`author`、`parent_version`
- 大小：`size_bytes`（内容字节长度）
- 令牌：`token_count`（tiktoken cl100k_base 令牌计数）
- 索引：`section_index`（JSON 数组，含 {anchor, level} 用于章节导航）

**ArtifactShare** — 分享链接，包含访问控制：
- `share_id`（shr_*）、`max_accesses`、`access_count`、`expires_at`、`is_revoked`

**ArtifactFolder** — 层级式产物组织

**ArtifactTag** — 标签，通过 `artifact_tag_map` 实现多对多

**ArtifactEvent** — 产物变更审计日志

## 安全

- **Fail-closed 认证**：未配置 API Key 时拒绝请求，除非 `allow_no_auth=True`
- **乐观锁**：通过 `expected_content_hash` 检测并发更新
- **路径穿越防护**：导出路径经过清洗
- **单租户边界**：引擎绑定 `127.0.0.1`，以单一共享 `X-API-Key` 认证，**无逐用户身份**——共享同一 Key 的所有调用方视为同一可信主体。**不要**将端口暴露到主机之外。多租户部署须在引擎前置认证代理，由其注入可信的 `caller_user_id`。
- **IDOR 防护 (v0.5.0)**：当 RPC 参数中传入 `caller_user_id` 时，写操作与 `artifact.get` 强制校验归属。若设置了 `caller_user_id` 且产物有 `owner_user_id`，二者必须一致，否则抛出 `PermissionError`（`-32006`，HTTP `403`）并拒绝操作。当省略 `caller_user_id`（单租户默认）或产物未设置归属时，跳过校验以保持向后兼容。覆盖方法：`artifact.get`、`artifact.get_content`、`artifact.update`、`artifact.patch`、`artifact.delete`、`artifact.version_rollback`。归属在创建时通过 `artifact.create` 的可选参数 `owner_user_id` / `ownership_type`（`free`/`project`/`cowork`）设定。

## 备份与恢复 (v0.4.2)

守护进程以 SQLite **WAL** 模式运行。后台线程周期性执行 `PASSIVE` checkpoint（默认每 300 秒），控制 `-wal` 文件增长；`close()` 时执行最终 `TRUNCATE` checkpoint，确保停机后 `meta.db` 自成一体、无残留 WAL 帧。

**在线备份** —— 守护进程运行时使用 `scripts/backup.sh`（不锁库，WAL 一致性快照）：

```bash
# 默认：~/.fusion/artifacts -> ~/.fusion/artifacts-backup-<时间戳>
./scripts/backup.sh

# 指定目标
./scripts/backup.sh /var/backups/artifacts-20260826

# 覆盖源 storage root
STORAGE_ROOT=/data/artifacts ./scripts/backup.sh /backup
```

脚本通过 `sqlite3 .backup` 对 `meta.db` 做在线一致性快照（不阻塞读写），并用 rsync 增量复制 `content/`。需 PATH 中有 `sqlite3` 与 `rsync`。

**恢复** —— 停止守护进程后，将快照 `meta.db` 与 `content/` 拷回 storage root：

```bash
./start.sh stop
rsync -a /var/backups/artifacts-20260826/meta.db ~/.fusion/artifacts/
rsync -a /var/backups/artifacts-20260826/content/ ~/.fusion/artifacts/content/
./start.sh start
```

**配置** —— 通过 `wal_checkpoint_interval`（秒；`0` 禁用后台线程，依赖 SQLite 默认 1000 页自动 checkpoint）调整 checkpoint 间隔：

```yaml
storage:
  wal_checkpoint_interval: 300   # env: FUSION_ARTIFACTS_WAL_CHECKPOINT_INTERVAL
```

**metadata 索引 (v0.5.0)** —— `metadata_indexed_keys` 列出高频 metadata 过滤字段名。对每个 key 建 `json_extract(metadata, '$.<key>')` 表达式索引，使 `metadata_filter` 命中索引而非全表扫。key 须为安全标识符（字母/下划线/数字），非法值被丢弃。留空=不建索引（向后兼容默认）。

```yaml
storage:
  metadata_indexed_keys: ["language", "framework"]
```

**游标分页 (v0.5.0)** —— `artifact.list_all` 接受可选 `cursor`（不透明，响应中以 `next_cursor` 返回）。配合 `updated_at` 或 `created_at` 排序时，引擎用 `WHERE (sort_col, id) < (cursor)` 游标查询替代 `OFFSET`，避免深分页扫+丢行的开销。其他排序或非法游标回退 OFFSET 分页。

**增量 token 计数 (v0.5.0)** —— `auto_compact` 与 `patch_artifact` 避免对大内容重复全量编码：
- 压缩器截断循环对每行 token 数只计一次，用前缀和遍历，每个 section 边界 O(1) 判定，不再每次重新拼接+全量编码（原先 O(n²)）。中间"是否已达预算"的判断先用廉价 `estimate_tokens` 估算门控，仅对候选结果调用精确编码器。
- `patch_artifact` 复用已持久化的 `version.token_count` 作为新旧内容 token 数，不再重新计数（3 次编码 → 1 次）。
- `auto_compact` 通过 `asyncio.to_thread` 把压缩+计数卸出事件循环，1MB+ 内容不阻塞其他请求。

**内容分块读写 (v0.5.0)** —— 大版本内容（>10KB，落盘存储）以 1MB 分块读写，替代原先一次性 `write_text`/`read_text` 全量加载字符串。无论内容多大，单次 I/O 峰值内存被约束在分块大小，避免 64 并发 10MB 请求下的 1.9–2.5GB OOM 峰值。分块写入遇到磁盘满（ENOSPC）映射为 `ResourceLimitError` 并清理半成品文件。

**SSE 事件体积上限 (v0.5.0)** —— `sse.max_event_bytes` 限制单个 SSE 事件序列化后字节数（默认 256KB）。超限事件被丢弃并记日志，不写入流，防单个大 payload 阻塞连接线程或撑爆客户端缓冲。`0` 禁用上限（向后兼容）。env 覆盖：`FUSION_ARTIFACTS_SSE_MAX_EVENT_BYTES`。

## 配置

```yaml
# default_config.yaml
server:
  host: "127.0.0.1"
  port: 11451

storage:
  root: "~/.fusion/artifacts"
  db_name: "meta.db"
  small_content_limit: 10240

thresholds:
  auto_create_lines: 30
  auto_create_chars: 1500

artifact:
  id_prefix: "art_"

security:
  allow_no_auth: false
  recycle_retention_days: 7

sse:
  heartbeat_interval: 30
  max_lifetime: 3600        # v0.4.2：单 SSE 连接最大存活秒；0=不限。到期服务端关流（发 __max_lifetime__）促客户端重连，防僵尸长连接占线程
  max_event_bytes: 262144   # v0.5.0：单个 SSE 事件序列化后最大字节；超限丢弃+告警。0=不限
```

## 架构

```
产品层: Fusion Code (tool_use) | Fusion Studio (GUI)
---------------------------------------------------------
中间件: Artifacts Engine (Python 守护进程, JSON-RPC 2.0 + REST /api/v1 + SSE)
---------------------------------------------------------
存储层: SQLite (WAL) + 文件系统
```

## 路线图

使 fusion-artifacts-engine 达到 Claude Artifacts 竞争力的重构蓝图（AR = Architecture Refactor）涵盖：

- 与 Claude Artifacts 的竞争力差距矩阵
- 数据模型扩展、新引擎操作（rename/star/pin/duplicate/snapshot/share/recycle/migrate-KB）、SSE 事件总线、REST `/api/v1` 对等、乐观锁
- 4 阶段实施计划（P1-P4）— **所有阶段已实现**

## 运行测试

```bash
source .venv/bin/activate
pytest tests/ -v
```

### 测试覆盖率

```bash
pytest tests/ --cov=fusion_artifacts_engine --cov-report=term-missing
```

当前覆盖率：**92%**，367+ 测试用例。

## 许可证

Apache License 2.0

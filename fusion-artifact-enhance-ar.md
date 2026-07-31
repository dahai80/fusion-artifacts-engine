# fusion-artifacts-engine 重构方案 (AR)

> **AR = Architecture Refactor**
> 版本：v1.0 ｜ 日期：2026-07-31
> 作者：fusion-artifacts-engine team
> 输入依据：`~/fusion/architecture/claude-artifacts-insight.md`（Claude Artifacts 竞品洞察 + Fusion Artifact PRD + 集成方案）+ 当前引擎实现源码
> 约束：本方案**仅修改 fusion-artifacts-engine 代码**；对上下游依赖（fusion-mlx / fusion-agent-studio / fusion-projects / fusion-cowork / fusion-studio）**只提 issue + PR 提案，不改其代码**；所有 GUI 需求以 md 格式设计提 issue 给 fusion-studio。

---

## 目录

1. [执行摘要与竞争定位](#1-执行摘要与竞争定位)
2. [现状分析](#2-现状分析)
3. [竞争力差距矩阵](#3-竞争力差距矩阵)
4. [重构目标与原则](#4-重构目标与原则)
5. [数据模型重构](#5-数据模型重构)
6. [引擎能力重构](#6-引擎能力重构)
7. [渲染层加固](#7-渲染层加固)
8. [```` ```artifact ```` 围栏语法解析](#8-artifact-围栏语法解析)
9. [API 层重构](#9-api-层重构)
10. [安全与隔离](#10-安全与隔离)
11. [分阶段实施计划](#11-分阶段实施计划)
12. [跨仓库协作清单（issue/PR 提案）](#12-跨仓库协作清单issuepr-提案)
13. [fusion-studio GUI 设计（md 原型）](#13-fusion-studio-gui-设计md-原型)
14. [测试策略](#14-测试策略)
15. [风险与缓解](#15-风险与缓解)

---

## 1. 执行摘要与竞争定位

### 1.1 一句话定位

fusion-artifacts-engine 要成为 **"可迭代、可分享、可沉淀、可治理"的 AI 产物引擎**--在 Claude Artifacts 的"对话即产物"基础上，补齐 Claude 的短板（无版本面板、无分类、Web 端无法持久化编辑、无模板、严格沙箱致无外部资源），并打出 Claude 没有的差异化（**命名快照+回滚**、**编辑器直存反向迭代**、**迁移项目知识库**、**事件总线 SSE**、**标签/文件夹分类**、**乐观锁并发治理**）。

### 1.2 竞争定位三角

```
                         Claude Artifacts
                        (对话即产物 / 体验流畅)
                              │
                  体验流畅 ───┼─── 治理与沉淀
                              │
   fusion-artifacts-engine ───┤
  (显式版本+迁移KB+事件总线+  │
   分类+并发治理+本地化渲染)  │
                              │
                    fusion-projects / fusion-agent-studio
                   (产物落地为项目知识 / Agent 驱动产物)
```

- **对 Claude 的"追平"项**：独立生命周期、```` ```artifact ```` 围栏、全局产物仓库、沙箱渲染、分享链接、回收站、收藏/置顶/重命名/复制。
- **对 Claude 的"超越"项**：命名快照+回滚、编辑器直存（反向迭代）、迁移项目知识库、SSE 事件总线、标签/文件夹、乐观锁。
- **生态协同项**（依赖兄弟仓库）：fusion-studio（GUI）、fusion-agent-studio（Agent 配置与工具）、fusion-projects（知识库沉淀）、fusion-mlx（token 计数 + 二期 Python 渲染）、fusion-cowork（协同共享）。

### 1.3 核心取舍声明（Rule 7：冲突只选一边）

| 冲突点 | 选择 | 理由 |
|---|---|---|
| 渲染外网资源（Mermaid CDN）vs 严格沙箱 | **严格沙箱 + 本地化** | 沙箱是安全底线，CDN 外网依赖违反隔离原则且离线不可用 |
| JSON-RPC vs REST | **两者并存**：内部 tool_use 走 RPC，外部/公开分享走 REST | RPC 已是 fusion-code/fusion-studio 既有契约；REST 为公开分享与直连必需 |
| 轮询 watch vs SSE | **SSE 为主，轮询兼容** | SSE 实时且省资源；轮询保留以兼容旧客户端 |
| 隐式版本 vs 显式快照 | **显式快照（命名）+ 自动版本** | Claude 隐式版本用户无感知；显式快照可命名、可回滚、可预览 |
| Python 渲染现在做 vs 二期 | **二期** | 需沙箱执行器，工程量大且与 fusion-mlx 评估耦合；先做 Markdown/HTML/SVG/Mermaid/React |

---

## 2. 现状分析

### 2.1 引擎能力清单（当前 v0.2.x）

**存储层**（`fusion_artifacts_engine/storage/sqlite_storage.py`）
- SQLite，WAL 模式，`check_same_thread=False`，`_write_lock = threading.Lock()` 串行写
- 表：`artifacts`、`artifact_versions`
- 小内容内联（< `small_content_limit` 10KB），大内容落盘 `content_path`
- 软删除 `is_deleted=1` / 硬删除 `DELETE + rmtree`
- 迁移模式：`_migrate_kind_column` / `_migrate_source_column` / `_migrate_project_id_column` / `_migrate_metadata_column`（新增列沿用此模式）
- `save_version` 遇 `IntegrityError`（version_num 冲突）重试

**模型层**（`fusion_artifacts_engine/models.py`）
- `ArtifactType = Literal["code","markdown","html","react","data"]`
- `ArtifactKind = Literal["app","code","document","game","tool","template"]`
- `Artifact`：id, session_id, name, type, kind, project_id, metadata, current_version, summary, created_at, updated_at, is_deleted
- `ArtifactVersion`：id, artifact_id, version_num, content, content_path, token_count, change_log, source(manual/ai_generation), created_at

**引擎层**（`fusion_artifacts_engine/engine.py`，438 行）
- CRUD：create / get / list(session 作用域) / delete(软)
- 版本：create_version / get_version_content / list_versions / rollback_version
- 注入与安全：inject / check_safety
- 自动识别：should_create_artifact / detect_artifact_type / extract_name_hint
- 导入导出：export_code / import_code / export_session / import
- 监听：register_watcher / unregister_watcher / get_watch_events（**轮询**）
- 双向同步：sync_artifact_file（正向 chat->update / 反向 interact user_edit->新版本）
- 渲染：render_artifact（HTML / SVG 包 HTML / Mermaid 包 **CDN** / React）、interact_artifact、get_artifact_raw_content
- `_RENDER_TYPE_MAP = {"html":"html","svg":"html","mermaid":"html","react":"react"}`

**RPC 层**（`fusion_artifacts_engine/rpc/methods.py` + `server.py`）
- JSON-RPC 2.0，`artifact.{create,get,get_content,list,delete,update,version_list,version_rollback,inject,check_safety,export,export_session,import,export_code,import_code,watch,sync,render,interact}` + `ping`
- REST：`/api/token-count`、`/api/artifact/render`、`/api/artifact/content/{id}`、`/api/artifact/interact`
- 鉴权：`FUSION_ARTIFACTS_API_KEY` + `hmac.compare_digest`
- `_MAX_BODY_SIZE = 10MB`；`_run_async` 用 `asyncio.run_coroutine_threadsafe`（30s 超时）

**自动识别层**（`fusion_artifacts_engine/auto_identifier.py`）
- `detect_renderable_type(content, name)` -> svg / mermaid / react / html
- `should_create_artifact`（按行数/字符阈值）、`detect_artifact_type`、`extract_name_hint`
- **缺失**：```` ```artifact ```` 围栏解析

**配置层**（`fusion_artifacts_engine/config.py`）
- `ArtifactEngineConfig`：storage_root, db_name, mlx_url, safe_context_threshold(180000), output_reserve_tokens(8192), auto_create_threshold_lines(30), auto_create_threshold_chars(1500), small_content_limit(10240), artifact_id_prefix("art_"), server_host("127.0.0.1"), server_port(8892)

### 2.2 既有差异化（已实现，需保留并强化）

- **显式版本 + 回滚**：`create_version` / `rollback_version`（Claude 无可见版本面板）
- **编辑器直存反向迭代**：`sync_artifact_file` 反向分支（Claude Web 端编辑无法持久化）
- **project_id 作用域**：已支持按项目隔离

### 2.3 现状缺口（对照 Claude + PRD）

1. 无 rename / star / pin / duplicate
2. 无全局跨会话产物列表（仅 session 作用域）
3. 无分享链接（只读公开访问）
4. 回收站只有软删除标志，无恢复入口、无 7 天过期清理
5. 无乐观锁（并发更新无冲突检测）
6. 无事件总线（仅轮询 watch）
7. Mermaid 走 CDN，违反沙箱外网隔离
8. 无 CSP 沙箱头
9. 无 Markdown 渲染输出
10. 无 ```` ```artifact ```` 围栏强制解析
11. 无标签 / 文件夹分类（仅 metadata 自由字段）
12. 无迁移项目知识库能力
13. 无命名快照（仅自动 version_num）

---

## 3. 竞争力差距矩阵

| # | 能力 | Claude Artifacts | 现状(v0.2.x) | 目标 | 差异化 |
|---|---|---|---|---|---|
| 1 | 独立生命周期产物 | ✅ | ✅ | ✅ | 持平 |
| 2 | 自动版本 | 隐式 | ✅自动 | ✅自动+命名快照 | **超越** |
| 3 | 版本可见面板 | ❌ | ✅列表 | ✅列表+预览+回滚 | **超越** |
| 4 | 编辑器直存(反向迭代) | ❌Web | ✅ | ✅ | **超越** |
| 5 | ```` ```artifact ```` 围栏 | ✅ | ❌ | ✅ | 追平 |
| 6 | 全局产物仓库(跨会话) | ✅ | ❌仅session | ✅ | 追平 |
| 7 | 沙箱渲染(CSP/禁外网) | ✅ | ⚠️CDN | ✅本地化+CSP | 追平 |
| 8 | 分享链接(只读) | ✅ | ❌ | ✅ | 追平 |
| 9 | 收藏/置顶/重命名/复制 | ✅ | ❌ | ✅ | 追平 |
| 10 | 回收站(7天恢复) | ✅ | ⚠️软删无恢复 | ✅ | 追平 |
| 11 | 迁移项目知识库 | ❌ | ❌ | ✅ | **差异化** |
| 12 | 事件总线(SSE) | - | ❌轮询 | ✅ | **差异化** |
| 13 | 标签/文件夹分类 | ❌ | ❌metadata | ✅ | **差异化** |
| 14 | 乐观锁(并发冲突) | - | ❌ | ✅ | **差异化** |
| 15 | Markdown 渲染 | ✅ | ❌ | ✅ | 追平 |
| 16 | Python 渲染 | ✅ | ❌ | 二期 | 二期 |
| 17 | 模板 | ✅ | kind=template | ✅强化 | 追平 |

**结论**：追平 9 项（#5,6,7,8,9,10,15,17 + 维持#1），超越 5 项（#2,3,4,11,12,13,14），二期 1 项（#16）。重构后 fusion-artifacts-engine 在"治理与沉淀"维度全面领先 Claude，在"体验"维度追平。

---

## 4. 重构目标与原则

### 4.1 目标

1. **可迭代**：命名快照 + 回滚 + 编辑器直存，产物可双向演进。
2. **可分享**：只读公开分享链接，无源码无版本泄漏。
3. **可沉淀**：迁移项目知识库，产物从对话流向长期资产。
4. **可治理**：全局仓库 + 标签/文件夹 + 回收站 + 乐观锁，可发现、可组织、可恢复、防冲突。
5. **可观察**：SSE 事件总线，产物变更实时推送。
6. **可隔离**：CSP 沙箱 + 本地化资源，渲染无外网依赖。

### 4.2 原则

- **向后兼容**：所有 schema 变更走 `_migrate_*` 列迁移，旧数据零停机；旧 RPC 方法不删，新方法叠加。
- **解耦**：引擎核心不依赖 GUI；事件总线解耦消费者。
- **沙箱优先**：渲染默认最严格隔离，放开需显式配置。
- **最小改动**（Rule 3）：只动必需文件，不重构未坏代码，匹配既有风格。
- **约定优于新颖**（Rule 11）：迁移、命名、RPC 风格沿用既有模式。
- **失败可见**（Rule 12）：迁移失败、冲突失败、分享失效均显式报错与日志。

---

## 5. 数据模型重构

### 5.1 `artifacts` 表新增列（向后兼容迁移）

| 列名 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `owner_user_id` | TEXT | NULL | 创建者用户 ID（多用户/协同） |
| `ownership_type` | TEXT | 'free' | free / project / cowork |
| `is_starred` | INTEGER | 0 | 收藏 |
| `is_pinned` | INTEGER | 0 | 置顶 |
| `pinned_chat_id` | TEXT | NULL | 置顶所属对话（一个产物可在多个对话置顶） |
| `share_id` | TEXT | NULL | 分享令牌（唯一索引） |
| `in_project_kb` | INTEGER | 0 | 是否已迁入项目知识库 |
| `folder_id` | TEXT | NULL | 所属文件夹 |
| `deleted_at` | TEXT | NULL | 软删除时间（回收站过期依据，ISO8601） |
| `content_hash` | TEXT | NULL | 当前内容 SHA256（乐观锁 + 去重） |
| `active_in_session` | TEXT | NULL | 当前活跃会话（全局仓库仍可跨会话访问） |

迁移方法：新增 `_migrate_ownership_column` / `_migrate_lifecycle_columns` / `_migrate_share_column` / `_migrate_kb_column` 等，沿用既有 `ALTER TABLE artifacts ADD COLUMN ... ; UPDATE artifacts SET ... WHERE ... IS NULL` 模式。日志记录迁移前后行数。

### 5.2 `artifact_versions` 表新增列

| 列名 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `snapshot_type` | TEXT | 'auto' | auto / manual / named |
| `snapshot_label` | TEXT | NULL | 命名快照的标签 |
| `author` | TEXT | NULL | 快照作者（user / agent_id） |
| `parent_version` | INTEGER | NULL | 父版本（分支演进） |

迁移：`_migrate_snapshot_columns`。

### 5.3 新增表

**`artifact_shares`**（分享管理）
```
share_id        TEXT PK
artifact_id     TEXT NOT NULL
created_by      TEXT
created_at      TEXT
expires_at      TEXT          -- 可选过期
revoked         INTEGER DEFAULT 0
access_count    INTEGER DEFAULT 0
last_access_at  TEXT
UNIQUE(share_id)
INDEX(artifact_id)
```

**`artifact_folders`**（文件夹分类）
```
folder_id   TEXT PK
name        TEXT NOT NULL
parent_id   TEXT              -- 支持嵌套
project_id  TEXT
created_at  TEXT
```

**`artifact_tags`**（标签，多对多）
```
tag_id      TEXT PK
name        TEXT NOT NULL UNIQUE
color       TEXT
```
**`artifact_tag_map`**
```
artifact_id TEXT NOT NULL
tag_id      TEXT NOT NULL
PRIMARY KEY(artifact_id, tag_id)
```

**`artifact_events`**（事件总线持久化 + SSE 重放）
```
event_id    TEXT PK
artifact_id TEXT
session_id  TEXT
event_type  TEXT NOT NULL     -- created/updated/snapshot_created/migrate_to_project_kb/deleted/restored
payload     TEXT              -- JSON
created_at  TEXT
INDEX(artifact_id, created_at)
INDEX(session_id, created_at)
```

### 5.4 索引调整

- `artifacts(share_id)` 唯一索引（公开访问快速定位）
- `artifacts(is_deleted, deleted_at)` 复合索引（回收站列表 + 过期清理）
- `artifacts(ownership_type, project_id)` 复合索引（全局仓库过滤）
- `artifacts(is_starred, is_pinned)` 复合索引（收藏/置顶列表）

---

## 6. 引擎能力重构

按能力域分组列出新增/增强方法。所有方法默认带日志（Rule：写代码必须默认有日志）。

### 6.1 生命周期管理

| 方法 | 说明 |
|---|---|
| `rename_artifact(artifact_id, new_name)` | 改名，校验同名冲突（同 session/project 内） |
| `star_artifact(artifact_id, starred: bool)` | 收藏切换 |
| `pin_artifact(artifact_id, chat_id, pinned: bool)` | 在指定对话置顶 |
| `duplicate_artifact(artifact_id, new_name=None)` | 复制：新 ID + 复制当前内容为 v1，**不继承历史版本** |

### 6.2 版本与快照

| 方法 | 说明 |
|---|---|
| `create_snapshot(artifact_id, label, author="user")` | 创建命名快照（snapshot_type='named'） |
| `list_snapshots(artifact_id)` | 仅返回 named/manual 快照（区别于自动版本流） |
| `rollback_version(...)` | **增强**：回滚后写一条 named 快照记录"回滚自 vX" |

### 6.3 全局产物仓库

| 方法 | 说明 |
|---|---|
| `list_all_artifacts(filters, sort, page)` | 跨会话全局列表；filters: ownership_type/project_id/type/tag/folder/starred/pinned/kind；sort: updated_at/created_at/name/starred；分页 |

### 6.4 分享

| 方法 | 说明 |
|---|---|
| `create_share(artifact_id, expires_at=None)` | 生成 share_id，写 artifact_shares |
| `get_shared_artifact(share_id)` | 公开只读：返回**渲染内容**，不返回源码、不返回版本、不返回元数据敏感字段；校验未撤销/未过期；累计 access_count |
| `revoke_share(share_id)` | 撤销 |

### 6.5 回收站

| 方法 | 说明 |
|---|---|
| `list_recycle(filters, page)` | 列出 is_deleted=1 且 deleted_at 在保留期内 |
| `restore_artifact(artifact_id)` | 恢复：is_deleted=0, deleted_at=NULL |
| `purge_expired(retention_days=7)` | 物理删除过期项（DELETE + rmtree content_dir） |

> 既有 `delete_artifact(soft=True)` 改为同时写 `deleted_at=now()`。

### 6.6 迁移项目知识库

| 方法 | 说明 |
|---|---|
| `move_to_project_kb(artifact_id, project_id)` | 置 in_project_kb=1 + project_id；发 `artifact.migrate_to_project_kb` 事件；fusion-projects 消费事件入库（见 §12） |

### 6.7 分类（标签/文件夹）

| 方法 | 说明 |
|---|---|
| `create_folder / list_folders / rename_folder / delete_folder` | 文件夹 CRUD |
| `add_tags / remove_tags / list_tags` | 标签多对多 |
| `move_to_folder(artifact_id, folder_id)` | 归类 |

### 6.8 并发治理（乐观锁）

- `update_artifact` / `create_version` 增 `expected_content_hash` 可选参数：传入则校验当前 `content_hash`，不匹配返回 `409 conflict`（携带当前 hash 供客户端合并）。
- `content_hash` 在每次内容变更后重算并写库。

### 6.9 事件总线

| 方法 | 说明 |
|---|---|
| `emit_event(artifact_id, event_type, payload)` | 写 artifact_events + 推送活跃 SSE 订阅 |
| `list_events(artifact_id=None, session_id=None, since_ts=None)` | 历史事件查询（重放） |

事件类型：`artifact.created` / `artifact.updated` / `artifact.snapshot_created` / `artifact.migrate_to_project_kb` / `artifact.deleted` / `artifact.restored` / `artifact.shared` / `artifact.renamed`。

---

## 7. 渲染层加固

### 7.1 CSP 沙箱头

`/api/artifact/content/{id}` 与公开分享端点响应头：
```
Content-Security-Policy: default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'none'; frame-ancestors 'self'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```
- `connect-src 'none'`：禁止外网请求（Claude 严格沙箱对齐）。
- 渲染产物在 `<iframe sandbox="allow-scripts">` 中加载（**不带 allow-same-origin**，防访问父上下文）。

### 7.2 Mermaid 本地化（消除 CDN）

- 将 `mermaid.min.js` 打包为 package_data（随 fusion-artifacts-engine 分发）。
- `render_artifact` 对 mermaid 类型：内联本地 `mermaid.min.js`（`<script>` 内嵌 base64 或 file 引用），**移除 CDN `integrity` 外链**。
- 离线可用，符合沙箱 `connect-src 'none'`。

### 7.3 Markdown 渲染

- 新增 render type `markdown`：打包 `marked.min.js`（本地），输出 HTML 文档（含基本排版样式）。
- `_RENDER_TYPE_MAP` 增 `"markdown":"html"`。

### 7.4 React 渲染

- 沿用现有 React 渲染，确保沙箱头生效。

### 7.5 Python 渲染（二期）

- 评估沙箱执行器（受限执行 + 静态图导出 matplotlib/plotly）。
- 提 issue 给 fusion-mlx 评估渲染服务（见 §12.4）。
- 二期实现，不阻塞本次重构。

### 7.6 渲染安全清单

- [ ] 所有渲染端点加 CSP 头
- [ ] iframe sandbox 无 allow-same-origin
- [ ] Mermaid/marked 本地化，无外网
- [ ] 公开分享无源码/版本泄漏
- [ ] 渲染失败返回明确错误页（不白屏）

---

## 8. ```` ```artifact ```` 围栏语法解析

### 8.1 语法

````
```artifact
name: my-dashboard
type: react
kind: app
---
<组件源码>
```
````

- 围栏标记 ```` ```artifact ```` 触发**强制创建产物**（区别于阈值自动判断）。
- 头部 YAML 可选字段：`name` / `type` / `kind` / `tags` / `folder`。
- `---` 分隔头部与内容；无头部则整体为内容，name/type 走自动识别。

### 8.2 实现

`auto_identifier.py` 新增 `parse_artifact_fence(text) -> list[ArtifactFenceBlock]`：
- 扫描所有 ```` ```artifact ```` 围栏块
- 解析头部 YAML（用 `yaml.safe_load`，失败则视为无头部）
- 返回 `(name, type, kind, tags, folder, content)` 列表

`should_create_artifact` 增强：若检测到围栏块，返回强信号（优先级高于阈值判断）。

### 8.3 与 Claude 对齐

Claude 用 ```` ```artifact ```` 强制产物化；本实现追平该语法，并扩展 `tags`/`folder` 头部（Claude 无）。

---

## 9. API 层重构

### 9.1 JSON-RPC 扩展（fusion-code tool_use + fusion-studio IPCClient）

新增方法（沿用 `RPCHandler._build_methods` 注册模式）：
```
artifact.rename / star / pin / duplicate
artifact.create_snapshot / list_snapshots
artifact.list_all              # 全局仓库
artifact.create_share / get_shared / revoke_share
artifact.list_recycle / restore / purge_expired
artifact.move_to_project_kb
artifact.create_folder / list_folders / rename_folder / delete_folder / move_to_folder
artifact.add_tags / remove_tags / list_tags
artifact.list_events           # 事件查询
artifact.update(增 expected_content_hash)  # 乐观锁
```
旧方法保留，不删除。

### 9.2 REST `/api/v1` 对齐（外部 + 公开分享）

```
GET    /api/v1/artifacts                       # 全局列表(filter/sort/page)
POST   /api/v1/artifacts                       # 创建
GET    /api/v1/artifacts/{id}                  # 详情
PATCH  /api/v1/artifacts/{id}                  # rename/star/pin/move(乐观锁)
DELETE /api/v1/artifacts/{id}                  # 软删->回收站
POST   /api/v1/artifacts/{id}/duplicate
POST   /api/v1/artifacts/{id}/snapshots        # 命名快照
GET    /api/v1/artifacts/{id}/snapshots
POST   /api/v1/artifacts/{id}/rollback
POST   /api/v1/artifacts/{id}/share            # create_share
DELETE /api/v1/artifacts/{id}/share            # revoke
GET    /api/v1/artifacts/{id}/content          # 源码(鉴权)
GET    /api/v1/artifacts/{id}/render           # 渲染(鉴权)
GET    /api/v1/recycle                         # 回收站
POST   /api/v1/recycle/{id}/restore
POST   /api/v1/artifacts/{id}/migrate-kb       # move_to_project_kb
GET    /api/v1/folders ; POST ; PATCH ; DELETE
POST   /api/v1/artifacts/{id}/tags ; DELETE
# 公开(无鉴权,只读渲染):
GET    /api/v1/share/{share_id}                # 渲染内容,无源码无版本
```
- 内部端点鉴权沿用 `FUSION_ARTIFACTS_API_KEY`。
- 公开分享端点无鉴权但只读渲染。

### 9.3 SSE 事件流

```
GET /api/v1/artifacts/{id}/events        # 单产物事件流(SSE)
GET /api/v1/sessions/{sid}/events        # 会话事件流(SSE)
```
- `Content-Type: text/event-stream`
- 客户端断线重连用 `Last-Event-ID` 头从 `artifact_events` 重放。
- 保留 `artifact.watch` 轮询方法兼容旧客户端。

---

## 10. 安全与隔离

> **现状安全债（v0.2.x，安全审计 2026-07-31 标记，需在 P1 一并修复）**：
> - **鉴权默认关闭**：`rpc/server.py` 中 `if _API_KEY:` 为 opt-in，env 未设时所有受保护端点完全开放。**修复**：改为 fail-closed（默认要求鉴权；显式 `--allow-no-auth` 仅限本地开发）。
> - **内容端点 IDOR + XSS 投递**：`_handle_artifact_content`（server.py:121）**无鉴权**，按 artifact_id 任意读取存储内容并以 `text/html` 返回 -> 任何本机进程可读任意产物 + 加载即执行其中脚本。**修复**：内容端点强制鉴权；渲染输出走 CSP sandbox iframe（见下），不直接以 text/html 服务到可脚本化的同源上下文。
> - **渲染内容无沙箱**：`engine.py:render_artifact` 将 SVG/Mermaid 内容原始拼入 HTML（line 379/384）。此为**设计意图**（HTML/React 产物本即可执行，转义会破坏渲染）；真正的缓解是**沙箱隔离**而非转义。

1. **鉴权**：内部 RPC/REST 强制 API Key（**fail-closed**，见上修复）；公开分享端点单独路由，无 Key 但只读渲染。
2. **沙箱**：渲染 CSP `connect-src 'none'`，iframe 无 `allow-same-origin`（消除上述 XSS 投递面）。
3. **资源本地化**：Mermaid/marked 打包分发，无 CDN 外网。
4. **分享最小泄漏**：公开端点只返回渲染 HTML，不返回源码/版本/owner。
5. **回收站保留期**：默认 7 天，可配置；过期物理删除。
6. **乐观锁**：并发更新冲突返回 409，不静默覆盖。
7. **输入校验**：type/kind 枚举校验沿用既有；share_id/folder_id/tag 名称做长度与字符校验。
8. **日志**：所有写操作记录 actor + before/after 摘要（不记完整内容，防日志膨胀）。
9. **绑定面收敛**：默认 `127.0.0.1` 仅回环；如需网络暴露必须同时启用鉴权 + TLS。

---

## 11. 分阶段实施计划

每期独立可发布，每期结束跑全量测试 + lint 全绿。

### P1 - 基础生命周期 + 全局仓库 + 回收站 + 乐观锁 + 鉴权加固
- 数据模型迁移（§5.1 / §5.2 列 + §5.4 索引）
- 引擎：rename/star/pin/duplicate/list_all/list_recycle/restore/purge_expired/乐观锁
- RPC + REST 对应方法
- **鉴权加固（安全债）**：fail-closed 鉴权；`/api/artifact/content/{id}` 强制鉴权修复 IDOR（§10）
- 测试：迁移、CRUD 新方法、乐观锁冲突、回收站过期、鉴权 fail-closed + 内容端点 401
- **成功标准**：旧数据迁移零损失；103+ 用例全绿；新方法单测覆盖；默认配置下无未鉴权受保护端点

### P2 - 渲染加固 + 围栏语法
- CSP 沙箱头 + iframe sandbox
- Mermaid/marked 本地化打包
- Markdown 渲染
- ```` ```artifact ```` 围栏解析
- 测试：CSP 头断言、离线渲染、围栏解析各形态
- **成功标准**：渲染无外网请求；围栏强制创建；Markdown 输出 HTML

### P3 - 分享 + 命名快照 + 迁移知识库 + REST /api/v1
- share/snapshot/move_to_project_kb
- REST /api/v1 全量对齐
- 公开分享端点
- 测试：分享只读无泄漏、快照命名回滚、迁移事件触发
- **成功标准**：公开分享无源码泄漏；命名快照可回滚；迁移事件可被消费

### P4 - 事件总线 SSE + 分类（标签/文件夹）
- SSE 流 + artifact_events 持久化 + 重放
- 标签/文件夹 CRUD
- 测试：SSE 推送、断线重放、标签多对多
- **成功标准**：SSE 实时推送；断线重放正确；分类过滤生效

> 跨仓库 issue 在 P1 启动时即提交（§12），PR 落地遵循各仓库 issue->PR->merge 流程，后续会话执行（本会话不改他人代码）。

---

## 12. 跨仓库协作清单（issue/PR 提案）

> 本会话**只提 issue**（遵循"不准修改别人代码"）。每个 issue 内附 PR 提案（分支名/改动文件/补丁大纲），供各仓库维护者评审合入。issue 链接在提交后回填本节。

### 12.1 fusion-studio（GUI，7 个 issue，每个含 md ASCII 原型 -> 详见 §13）

issue 已提交（2026-07-31），链接见下表。

| # | 标题 | 要点 | issue 链接 |
|---|---|---|---|
| S1 | 全局 Artifacts 仓库页 | 侧边入口、网格列表、搜索/排序/筛选、卡片菜单 | [#20](https://github.com/dahai80/fusion-studio/issues/20) |
| S2 | 对话内 Artifact 预览卡片 | 缩略、Open/Copy/Download/Pin、菜单 | [#21](https://github.com/dahai80/fusion-studio/issues/21) |
| S3 | Artifact 全屏画布 | Preview/Code Tab、工具栏、Star/Share、版本历史 | [#22](https://github.com/dahai80/fusion-studio/issues/22) |
| S4 | 版本历史面板 | 快照列表、预览、回滚 | [#23](https://github.com/dahai80/fusion-studio/issues/23) |
| S5 | 分享弹窗 + Code Tab Save 直存 | 分享链接生成/复制/撤销；编辑器直存 | [#24](https://github.com/dahai80/fusion-studio/issues/24) |
| S6 | 迁移至 Project 操作入口 | move_to_project_kb 触发 + 状态 | [#25](https://github.com/dahai80/fusion-studio/issues/25) |
| S7 | IPCClient 扩展对接新 RPC/REST/SSE | 新方法注册、REST 直连、SSE 订阅 | [#26](https://github.com/dahai80/fusion-studio/issues/26) |

> PR 提案（每 issue 内附）：分支 `feat/artifacts-{issue#}`，改 `Sources/FusionStudio/.../IPCClient.swift`、`Artifacts/*` 视图、`docs/upstream-http-endpoints.md`。

### 12.2 fusion-agent-studio（3 个 issue）

| # | 标题 | 要点 | issue 链接 |
|---|---|---|---|
| A1 | Agent 产物配置 | 创建/更新开关、触发策略、Prompt 合并、工具权限 | [#32](https://github.com/dahai80/fusion-agent-studio/issues/32) |
| A2 | Agent Function Calling 工具 | artifact_get_source / artifact_update / artifact_create | [#33](https://github.com/dahai80/fusion-agent-studio/issues/33) |
| A3 | 活跃 Artifact 上下文注入 | 迭代时注入当前源码到上下文 | [#34](https://github.com/dahai80/fusion-agent-studio/issues/34) |

> PR 提案：分支 `feat/artifact-agent-{issue#}`。

### 12.3 fusion-projects（2 个 issue，仓库待创建）

> `dahai80/fusion-projects` GitHub 仓库尚不存在（本地仅有 scaffold）。issue 待仓库创建后提交；提案先记录于此。

| # | 标题 | 要点 |
|---|---|---|
| P1 | Artifact 迁移入库接口 | 消费 `artifact.migrate_to_project_kb` 事件，建文档 + 向量索引 |
| P2 | 项目产物列表/导出关联 Artifact | 项目页展示关联产物、导出 |

### 12.4 fusion-mlx（1 个 issue）

| # | 标题 | 要点 | issue 链接 |
|---|---|---|---|
| M1 | count_tokens 端点确认 + Python 渲染沙箱评估 | 确认 count_tokens 可用；评估 matplotlib/plotly 静态图渲染服务 | [#291](https://github.com/dahai80/fusion-mlx/issues/291) |

### 12.5 fusion-cowork（1 个 issue）

| # | 标题 | 要点 | issue 链接 |
|---|---|---|---|
| C1 | 协同会话 Artifact 只读共享 + 创建者编辑权限 | 多人协同只读、创建者可编辑、权限流转 | [#3](https://github.com/dahai80/fusion-cowork/issues/3) |

### 12.6 依赖与优先级

```
fusion-artifacts-engine (本仓库, P1-P4)
   │
   ├── fusion-studio (GUI, S1-S7)           ── 依赖 RPC/REST/SSE 契约
   ├── fusion-agent-studio (A1-A3)          ── 依赖 RPC 契约
   ├── fusion-projects (P1-P2)              ── 依赖 migrate 事件
   ├── fusion-mlx (M1)                      ── count_tokens(已用) + 渲染评估
   └── fusion-cowork (C1)                   ── 依赖 share/权限契约
```

---

## 13. fusion-studio GUI 设计（md 原型）

> 以下 ASCII 原型随 fusion-studio issue 一并提交。SwiftUI 实现由 fusion-studio 维护者负责（本仓库不改其代码）。

### 13.1 S1 - 全局 Artifacts 仓库页

```
┌─────────────────────────────────────────────────────────────┐
│ Fusion Studio          [🔍 搜索产物…]        ⚙️        👤    │
├──────┬──────────────────────────────────────────────────────┤
│ 侧栏 │ Artifacts 仓库                          [+ 新建] [⟳]  │
│ ▸对话│ 筛选: [类型▾] [标签▾] [文件夹▾] [⭐仅收藏] [📌仅置顶] │
│ ▸产物│ 排序: [更新时间▾]  视图: [⊞网格] [☰列表]              │
│  仓库├──────────────────────────────────────────────────────┤
│ ▸项目│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│ ▸知识│ │ Dashboard│ │ 落地页demo│ │ 数据图表 │ │ 贪吃蛇   │  │
│      │ │ react·app│ │ html·app │ │ mermaid  │ │ react·game│ │
│      │ │ ⭐ 📌     │ │          │ │ ⭐        │ │          │  │
│      │ │ 2m前     │ │ 1h前     │ │ 昨天     │ │ 3天前    │  │
│      │ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│      │ ┌──────────┐ ┌──────────┐                             │
│      │ │ API 文档  │ │ 营销文案 │   < 1 2 3 >  共 24 项      │
│      │ │ md·doc   │ │ md·doc  │                             │
│      │ └──────────┘ └──────────┘                             │
└──────┴──────────────────────────────────────────────────────┘
卡片菜单(右键/长按): 打开 | 复制 | 下载 | 重命名 | 收藏 | 置顶 |
                    复制为新 | 分享 | 迁移至项目 | 删除(回回收站)
```
**交互**：搜索实时过滤；筛选多选叠加；网格/列表切换；卡片悬停显示菜单；分页/无限滚动。空态引导"从对话创建产物"。

### 13.2 S2 - 对话内 Artifact 预览卡片

```
┌─ 对话流 ────────────────────────────────────────────────────┐
│ user: 帮我做一个销售仪表盘                                   │
│ assistant: 已创建产物 ↓                                     │
│ ┌─ Artifact 卡片 ──────────────────────────────────────┐    │
│ │ 📊 销售仪表盘          react · app        v3 · 2m前  │    │
│ │ ┌──────────────────────────────────────────────┐    │    │
│ │ │ [缩略预览: 仪表盘渲染缩略图]                  │    │    │
│ │ └──────────────────────────────────────────────┘    │    │
│ │ [打开画布] [复制源码] [下载] [📌置顶] [⋯ 更多]      │    │
│ └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
更多菜单: 重命名 | 收藏 | 复制为新 | 分享 | 迁移至项目 | 版本历史 | 删除
```
**交互**：卡片内联渲染缩略；"打开画布"进 S3；操作后卡片状态实时刷新（SSE）。

### 13.3 S3 - Artifact 全屏画布

```
┌─────────────────────────────────────────────────────────────┐
│ ◄ 返回   📊 销售仪表盘  [✏️重命名]   ⭐  📌  🔗分享  ⋯      │
├─────────────────────────────────────────────────────────────┤
│ [Preview] [Code]                    v3 ▾   [💾 保存] [⟲回滚]│
├─────────────────────────────────────────────────────────────┤
│                                                              │
│              [ 沙箱渲染区域 (iframe sandbox) ]               │
│                                                              │
│                                                              │
├─────────────────────────────────────────────────────────────┤
│ 状态: 已保存 · v3 · 2m前 · token 1,204   [版本历史 ▾]        │
└─────────────────────────────────────────────────────────────┘
Code Tab: 编辑器(等宽) + 保存按钮 -> 编辑器直存(反向迭代, 触发新版本)
版本历史 ▾: 展开 S4 面板
分享: 弹出 S5
```
**交互**：Preview/Code 切换；Code 编辑后 Save 触发 `sync` 反向迭代；冲突（乐观锁 409）弹合并对话框。

### 13.4 S4 - 版本历史面板

```
┌─ 版本历史: 销售仪表盘 ─────────────────────┐
│ [命名快照] [全部版本]                      │
├────────────────────────────────────────────┤
│ ● v3  "发布候选" 🏷️named   2m前   [预览][⟲回滚]│
│ │                                          │
│ ● v2  auto                 1h前   [预览][⟲]  │
│ │                                          │
│ ● v1  "初版" 🏷️named      昨天   [预览][⟲]  │
└────────────────────────────────────────────┘
预览: 右侧浮层渲染该版本(只读)
回滚: 确认对话框 -> 回滚后自动建一条 named 快照"回滚自 vX"
```

### 13.5 S5 - 分享弹窗 + Code Tab Save 直存

```
分享弹窗:
┌─ 分享: 销售仪表盘 ────────────────┐
│ 分享链接(只读渲染, 无源码):       │
│ ┌──────────────────────────────┐ │
│ │ https://.../s/aB3xK9         │ │
│ └──────────────────────────────┘ │
│ [📋复制] [打开] [🗑️撤销分享]      │
│ 过期: [7天▾] [访问次数: 12]       │
└──────────────────────────────────┘

Code Tab Save 直存:
[Code]编辑 -> [💾保存] -> "已保存为新版本 v4" (反向迭代)
冲突时: ⚠️ 内容已被他人修改, [查看差异] [覆盖] [取消]
```

### 13.6 S6 - 迁移至 Project 操作入口

```
菜单/工具栏: 迁移至项目 ▾
┌─ 迁移至项目知识库 ────────────────┐
│ 目标项目: [我的产品项目 ▾]        │
│ ☑ 同时保留在仓库(可继续迭代)     │
│ [取消] [迁移]                     │
└──────────────────────────────────┘
迁移中: ⏳ 触发 move_to_project_kb...
完成: ✅ 已迁入「我的产品项目」知识库 (监听 migrate 事件)
失败: ❌ 迁移失败: <原因> [重试]
产物卡片/列表显示 📚 已入库 角标
```

### 13.7 S7 - IPCClient 扩展对接

```
fusion-studio IPCClient 新增对接:
- JSON-RPC: artifact.{rename,star,pin,duplicate,create_snapshot,
            list_snapshots,list_all,create_share,revoke_share,
            list_recycle,restore,move_to_project_kb,add_tags,...}
- REST /api/v1: 公开分享 GET /api/v1/share/{share_id} (无鉴权直连)
- SSE: 订阅 /api/v1/artifacts/{id}/events (实时刷新卡片/画布状态)
文档: 更新 docs/upstream-http-endpoints.md 新方法表 + SSE 契约
```

---

## 14. 测试策略

### 14.1 单元测试
- 数据迁移：旧库 -> 新库字段默认值正确、行数一致、回滚迁移可重入
- 新引擎方法：rename/star/pin/duplicate/snapshot/share/recycle/migrate/tags/folder
- 乐观锁：并发 update 冲突返回 409、expected_hash 匹配则成功
- 围栏解析：有头/无头/多块/恶意头部（yaml 注入）
- 渲染：CSP 头断言、Mermaid/marked 本地化无外网、Markdown 输出 HTML
- 分享：公开端点无源码/版本泄漏、撤销/过期失效、access_count 累计
- SSE：推送、断线 Last-Event-ID 重放

### 14.2 集成测试
- 端到端：创建->编辑直存->命名快照->回滚->分享->公开访问->迁入项目->事件推送
- 回收站：软删->列表->恢复->过期清理物理删除
- 全局仓库：跨会话列表、筛选/排序/分页

### 14.3 兼容测试
- 旧 RPC 方法不回归（103 现有用例全绿）
- 旧 `watch` 轮询与 SSE 并存
- 旧客户端无 expected_hash 时退化为非乐观锁

### 14.4 安全测试
- 沙箱 iframe 无法访问父上下文
- 公开分享端点无法获取源码/版本/owner
- CSP 阻止外网请求（Mermaid 不再请求 CDN）

---

## 15. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 迁移破坏旧数据 | 高 | _migrate_* 模式幂等；迁移前后行数校验日志；先备份 DB |
| Mermaid 本地化包体积 | 中 | 用 min 版；按需；评估 tree-shake |
| SSE 长连接资源 | 中 | 超时断开；心跳；限制每客户端连接数 |
| 公开分享被滥用 | 中 | 速率限制；可选过期；撤销即失效；无敏感元数据 |
| 乐观锁误伤 | 低 | expected_hash 可选；冲突返回当前 hash 供合并 |
| 跨仓库 issue 滞后 | 中 | 本会话即提 issue；PR 提案写入 issue body；定期跟进 |
| Python 渲染沙箱安全 | 高(二期) | 二期；受限执行；静态图导出；fusion-mlx 评估 |
| fusion-projects 仓库不存在 | 低 | 提案记录于 §12.3；待仓库创建后提 issue |
| 鉴权默认关闭 + 内容端点 IDOR（v0.2.x 安全债） | 高 | P1 鉴权加固：fail-closed + 内容端点强制鉴权（§10）；虽仅绑 127.0.0.1 仍防本机进程/SSRF |

---

## 附录 A：与 Claude Artifacts 的逐项对照

（见 §3 竞争力差距矩阵）

## 附录 B：变更文件清单（本仓库，实施阶段）

| 文件 | 变更 |
|---|---|
| `fusion_artifacts_engine/models.py` | 新增字段、新模型(Share/Folder/Tag/Event) |
| `fusion_artifacts_engine/storage/sqlite_storage.py` | 新列迁移、新表、新索引、新 CRUD |
| `fusion_artifacts_engine/engine.py` | 新能力方法、事件总线、乐观锁 |
| `fusion_artifacts_engine/auto_identifier.py` | 围栏解析 |
| `fusion_artifacts_engine/rpc/methods.py` | 新 RPC 方法注册 |
| `fusion_artifacts_engine/rpc/server.py` | REST /api/v1 路由、SSE、CSP 头、公开分享端点 |
| `fusion_artifacts_engine/config.py` | 新配置项(回收站保留期、SSE 心跳等) |
| `package_data` | mermaid.min.js、marked.min.js |
| `tests/` | 新测试用例 |
| `README.md` | 指向本 AR 文档 |

---

*本 AR 文档为 fusion-artifacts-engine 重构蓝图。实施按 §11 分阶段推进；跨仓库协作按 §12 提 issue + PR 提案。*

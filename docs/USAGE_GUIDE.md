# fusion-artifacts-engine 使用指南 / Usage Guide

> 版本 / Version: v0.4.1
> 中英双语 / Bilingual (Chinese + English)

<!-- 中文部分 -->

## 中文部分

### 1. 这是什么

`fusion-artifacts-engine` 是 Fusion 架构里的**结构化产物中间件**。它把 AI 对话中生成的大段内容（代码、文档、HTML 应用）从聊天消息里**物理分离**出来，单独存成一个 artifact，对话里只保留一个轻量引用（约 30 token）。需要内容时再按需取回。

**为什么需要它**：长对话里反复迭代 1000 行代码，10 轮下来对话上下文会膨胀到约 120k token。用 artifact 引用替换后，同样 10 轮只占约 15k token，省约 87% 上下文。

**核心能力**：
- 产物 CRUD + 版本管理（每次更新自动生成新版本，保留历史）
- 自动渲染检测（HTML / React / SVG / Mermaid / Markdown）
- 公开分享（只读链接，带过期和访问次数限制）
- 上下文预算与安全检查
- 软删除 + 回收站
- 运维探针（限流、指标、健康/就绪检查）

**在 Fusion 里的位置**：被 **fusion-studio**（GUI 客户端，启动时自动拉起本服务）和 **fusion-code**（工具调用）调用。本服务监听 `127.0.0.1:11451`，同一端口暴露三套接口：JSON-RPC 2.0（POST `/`）、REST `/api/v1/*`、SSE `/api/v1/events/stream`。

### 2. 快速开始

**前置**：激活仓库根 venv（所有 Python 工作前必做）。

```bash
cd /Users/dahai/fusion
source .venv/bin/activate
cd fusion-artifacts-engine
pip install -e ".[all]"
```

**启动守护进程**（推荐方式，`start.sh` 会等健康检查通过再返回）：

```bash
./start.sh start      # 启动，监听 127.0.0.1:11451
./start.sh status     # 健康则 exit 0，否则 exit 1
./start.sh stop
./start.sh restart
```

或直接用 CLI：

```bash
fusion-artifacts-engine start --host 127.0.0.1 --port 11451
fusion-artifacts-engine status
fusion-artifacts-engine version
```

**验证服务起来**（发一个 ping）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

期望返回：`{"jsonrpc":"2.0","id":1,"result":{"pong":true,"version":"0.4.1"}}`

**鉴权说明**：默认 fail-closed（必须带 `X-API-Key`）。设环境变量 `FUSION_ARTIFACTS_API_KEY=你的密钥` 后，每个请求带头：

```bash
curl -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" ...
```

本地测试可设 `allow_no_auth=true`（配置文件或代码里）跳过鉴权。`/healthz`、`/readyz`、`/metrics`、`/api/v1/share/*` 不需要鉴权。

**第一个 artifact**（JSON-RPC 创建）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.create",
    "params":{
      "session_id":"demo-001",
      "name":"hello.py",
      "type":"code",
      "content":"def hello():\n    print(\\"hello fusion\\")\n",
      "summary":"示例 hello 函数"
    }
  }'
```

返回里 `result.ref_text` 就是那个约 30 token 的引用，长这样：

```
[Artifact: hello.py | ID: art_xxxx | Version: v1 | Type: code | Size: 42 | Summary: 示例 hello 函数]
```

把这个 `ref_text` 放进对话，替代原始代码。需要内容时用 `artifact.get_content` 取回。

### 3. 场景一：长代码存成 Artifact（替换对话中的大段内容）

**场景**：AI 生成了一段 800 行的 Python 脚本。直接塞对话会吃掉大量上下文。

**做法**：调用 `artifact.create`，拿到 `ref_text`，在对话里只保留引用。

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.create",
    "params":{
      "session_id":"proj-2026-08",
      "name":"data_pipeline.py",
      "type":"code",
      "content":"<你的 800 行代码>",
      "summary":"ETL 数据清洗管线",
      "kind":"code",
      "project_id":"proj-2026-08",
      "metadata":{"lang":"python","author":"dahai"}
    }
  }'
```

**参数说明**：
| 参数 | 必填 | 说明 |
|------|------|------|
| `session_id` | 是 | 会话标识，用于归集同一对话的产物 |
| `name` | 是 | 产物名（如 `data_pipeline.py`），影响类型推断 |
| `type` | 是 | `code` / `markdown` / `html` / `react` / `data` |
| `content` | 是 | 内容全文 |
| `summary` | 否 | 简短描述（进 ref_text，≤200 字符） |
| `kind` | 否 | `app` / `code` / `document` / `game` / `tool` / `template`，不传则自动推断 |
| `project_id` | 否 | 项目标识，用于跨会话归集 |
| `metadata` | 否 | 自定义元数据（JSON 对象，可按字段过滤） |
| `change_log` | 否 | 本次变更说明，不传则自动从 diff 生成 |

**返回**：`{artifact, version, ref_text}`。其中 `artifact.id` 形如 `art_a1b2c3`，后续所有操作靠它。

**关键点**：
- 小内容（<10KB，可配）直接存进 SQLite；大内容落盘到 `content/{art_id}/v{num}.{ext}`。
- 第一版 `change_log` 默认 `"Initial version"`。
- 取回内容用 `artifact.get_content`，`version` 传 `"latest"` 或版本号。

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":2,"method":"artifact.get_content",
    "params":{"artifact_id":"art_a1b2c3","version":"latest"}
  }'
```

返回：`{content, token_count, version}`。

**引导式提示**：如果你是第一次用，先跑通第 2 节的 ping 和第一个 artifact，确认服务正常、鉴权通了，再做本场景。

### 4. 场景二：版本迭代（更新、版本对比、回滚）

**场景**：AI 第二轮改了那段代码，第三轮又改。每轮都应存成新版本，而不是覆盖。

**更新内容**（`artifact.update`，自动生成 v2）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.update",
    "params":{
      "artifact_id":"art_a1b2c3",
      "content":"<改后的代码>",
      "change_log":"加入异常重试",
      "source":"ai_generation"
    }
  }'
```

**参数**：
| 参数 | 必填 | 说明 |
|------|------|------|
| `artifact_id` | 是 | 产物 ID |
| `content` | 是 | 新内容全文 |
| `change_log` | 否 | 变更说明，不传自动从 diff 生成 |
| `source` | 否 | `manual`（人工）或 `ai_generation`（AI 生成），记录来源 |
| `expected_content_hash` | 否 | 乐观锁：传入当前内容哈希，不匹配则报错（防并发覆盖丢失更新） |

返回 `{version, ref_text}`，`version.version_num` 递增。

**版本对比**（看 v2 和 v5 差了啥）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.version_diff",
    "params":{"artifact_id":"art_a1b2c3","from_version":2,"to_version":5}
  }'
```

返回 diff 结果。配合 `artifact.version_list` 列出所有版本、`artifact.version_rollback` 回滚到指定版本。

**回滚**（把当前版本退回 v3，回滚会生成一个新版本标记为 rollback）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.version_rollback",
    "params":{"artifact_id":"art_a1b2c3","target_version":3}
  }'
```

返回 `{version, ref_text}`，`version.version_num` 是回滚生成的新版本号（非目标版本号本身，历史不丢）。

**引导式提示**：乐观锁在多端并发编辑同一 artifact 时有用——`expected_content_hash` 传入你取内容时拿到的哈希，若期间别人改过，服务端报 Conflict（可重试），避免你的更新覆盖别人的改动。

### 5. 场景三：自动渲染检测（render）

**场景**：AI 输出了一段 HTML 或 Mermaid 图，你想判断它该不该存成 artifact、能不能在客户端渲染。

**做法**：调用 `artifact.render`。它做四件事——阈值判定、类型检测、存成 artifact、返回 `render_type` 提示。**不执行浏览器渲染**（渲染由客户端 fusion-studio 负责）。

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.render",
    "params":{
      "content":"<!DOCTYPE html><html><body><h1>Hello</h1><p>'"$(python3 -c "print('lorem '*400)")"'</p></body></html>",
      "session_id":"demo-001",
      "lang_hint":"html"
    }
  }'
```

> 示例里用 `python3 -c "print('lorem '*400)"` 拼出超 1500 字符的内容，确保跨过阈值。直接传短 HTML（如只有一行 `<h1>`）会因低于阈值返回 `below_threshold`。

**返回**：
- 内容超阈值且可渲染：`{created: true, artifact: {...}, render_type: "html", content, ref_text}`
- 内容太短（行数 <30 且字符 <1500）：`{created: false, reason: "below_threshold"}`
- 空内容：`{created: false, reason: "empty_content"}`
- 失败：`{created: false, reason: "<错误信息>"}`

`render_type` 可能值：`html` / `react` / `svg` / `mermaid` / `markdown`。客户端据此决定怎么渲染。不可渲染时 `render_type` 为 `null`。

**判定阈值**（`default_config.yaml` 可配）：
- 行数 ≥ 30 或字符数 ≥ 1500 才会创建 artifact（低于阈值直接跳过）。
- 可渲染类型检测基于内容特征（如含 `<!DOCTYPE html>` 判 html，含 `mermaid` 关键字判 mermaid）。

**引导式提示**：`render` 和 `create` 的区别——`create` 你明确知道要存；`render` 给它一堆内容让它自己判断该不该存、是什么类型。客户端拿 AI 输出时，不确定该不该存成 artifact，就用 `render`。

### 6. 场景四：分享 Artifact（share）

**场景**：要把一个 artifact 以只读链接形式公开分享，设过期时间或访问次数上限。

**创建分享**：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.create_share",
    "params":{
      "artifact_id":"art_a1b2c3",
      "created_by":"dahai",
      "expires_at":"2026-09-24T00:00:00Z",
      "max_accesses":100
    }
  }'
```

返回 `{share}`，`share.share_id` 形如 `shr_xxxx`。分享链接（REST，免鉴权）：

```
http://127.0.0.1:11451/api/v1/share/shr_xxxx
```

浏览器打开即看到渲染后的只读预览（服务端渲染，带严格 CSP 头防 XSS）。share 过期或达到次数上限返回 410 Gone；不存在返回 404。

**相关操作**：
- `artifact.get_shared`：按 share ID 取分享详情。
- `artifact.revoke_share`：撤销分享（删后链接即 404）。

**约束**：分享最大 TTL 由 `share_max_ttl_days`（默认 90 天）限制。

**引导式提示**：share 接口走独立限流桶（`public_rate_limit_rps`），和受鉴权的 RPC 限流分开，避免公开链接的流量拖垮正常 API。生产环境务必给公开桶设合理上限。

### 7. 场景五：上下文预算与安全检查

**场景**：发请求给大模型前，想知道当前对话还剩多少 token 预算，或确认内容没超限。

**上下文预算**（`context.budget`）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"context.budget",
    "params":{"session_id":"demo-001","context_window":200000}
  }'
```

返回当前 token 占用、剩余预算、是否安全。

**安全检查**（`artifact.check_safety`）：传一组 messages 和输出预算，判断是否超限。

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.check_safety",
    "params":{"messages":[{"role":"user","content":"hello"}],"output_budget":10000}
  }'
```

返回 `{safe: true/false, current_tokens, remaining_tokens}`。`remaining_tokens < 0` 表示超限。

**自动压缩**（`artifact.auto_compact`）：一个 artifact 太长，给个 token 预算让它压缩。

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.auto_compact",
    "params":{"artifact_id":"art_a1b2c3","token_budget":4000}
  }'
```

**引导式提示**：`context.budget` 看全局预算，`check_safety` 看具体一组 messages 安不安全，`auto_compact` 在超限时主动压缩某个 artifact。三者配合：先 budget 看余量 → check_safety 验证即将发送的内容 → 超了就 auto_compact 压缩占大头的 artifact。

**注意**：`artifact.inject` 和 `artifact.interact` 已在 v0.4.0（运维批次 6）**下线**，RPC 层返回 `-32005 NotImplementedError`。安全上下文管理改用 `context.budget` + `auto_compact`。

### 8. 场景六：生命周期管理（重命名、收藏、置顶、复制）

**场景**：产物多了需要整理——改名、标星、置顶、复制一份。

**重命名**：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.rename",
    "params":{"artifact_id":"art_a1b2c3","new_name":"pipeline_v2.py"}
  }'
```

**收藏 / 置顶**：

```bash
# 收藏
curl -s -X POST http://127.0.0.1:11451/ -H "Content-Type: application/json" -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.star","params":{"artifact_id":"art_a1b2c3"}}'

# 置顶
curl -s -X POST http://127.0.0.1:11451/ -H "Content-Type: application/json" -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.pin","params":{"artifact_id":"art_a1b2c3"}}'
```

**复制**（生成一个新 artifact，内容相同、新 ID）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.duplicate","params":{"artifact_id":"art_a1b2c3"}}'
```

**归集整理相关**：
- `artifact.create_folder` / `list_folders` / `rename_folder` / `delete_folder` / `move_to_folder`：文件夹组织。
- `artifact.add_tag` / `remove_tag` / `list_tags` / `list_artifact_tags`：标签。
- `artifact.list`：按 `session_id` / `project_id` / `metadata_filter` 过滤列出。
- `artifact.list_all`：列出全部。
- `artifact.move_to_project_kb`：把产物挪到项目知识库。

**引导式提示**：`metadata`（创建时传的 JSON）可用 `metadata_filter` 在 `list` 里按字段过滤，例如只列 `lang=python` 的产物。这是比文件夹更灵活的归集方式，适合自定义分类。

### 9. 场景七：回收站（软删除、恢复、清理）

**场景**：删产物但不真删，先放回收站，后悔能恢复。

**软删除**（默认行为，`soft_delete` 默认 `true`）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.delete",
    "params":{"artifact_id":"art_a1b2c3","soft_delete":true}
  }'
```

返回 `{ok: true}`。软删后 `artifact.get` 默认查不到，但 `artifact.list_recycle` 能列回收站里的。

**恢复**：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.restore","params":{"artifact_id":"art_a1b2c3"}}'
```

**彻底清理**（回收站里超过保留期的自动清掉）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.purge_expired","params":{}}'
```

**硬删除**（直接删，不进回收站）：

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.delete",
    "params":{"artifact_id":"art_a1b2c3","soft_delete":false}
  }'
```

**约束**：回收站保留期由 `recycle_retention_days`（默认 7 天）控制。过期产物由 `purge_expired` 清理。

**引导式提示**：除非确定不要，始终用软删（默认）。`purge_expired` 是定时清理回收站的手段，生产环境建议配定时任务调用。硬删不可逆，谨慎。

### 10. 场景八：运维探针（健康检查、指标、就绪）

**场景**：生产部署后，K8s/负载均衡要探测服务状态、Prometheus 抓指标。

**存活探针**（`/healthz`，免鉴权，始终 200）：

```bash
curl -s http://127.0.0.1:11451/healthz
# {"status":"ok","check":"liveness"}
```

只要进程在跑就返回 200，用于判断进程存活。

**就绪探针**（`/readyz`，免鉴权，依赖就绪才 200，否则 503）：

```bash
curl -s http://127.0.0.1:11451/readyz
# 就绪: {"status":"ready","checks":{"storage":true,"event_bus":true}}
# 未就绪: {"status":"not_ready","checks":{"storage":false,...}}  (HTTP 503)
```

检查 `storage.health_check()` 和 `event_bus` 状态。存储或事件总线挂了返回 503，负载均衡应把流量切走。

**指标**（`/metrics`，免鉴权，Prometheus 文本格式）：

```bash
curl -s http://127.0.0.1:11451/metrics
```

输出含 `rpc_requests_total`（counter）、`rpc_active_conns`（gauge）等。`metrics_enabled=false` 时返回 404。

**限流**（v0.4.0 运维批次 1）：令牌桶算法。超限返回 HTTP 429，JSON-RPC 错误体 `-32003`、`retryable: true`。

```bash
# 超限响应示例
{"jsonrpc":"2.0","id":1,"error":{"code":-32003,"message":"rate limit exceeded","retryable":true}}
```

限流分两个桶：
- 默认桶（受鉴权 RPC）：`rate_limit_rps` / `rate_limit_burst`，`rps=0` 表示不限。
- 公开桶（share 接口）：`public_rate_limit_rps` / `public_rate_limit_burst`。

**磁盘检查**（运维批次 4）：写内容前检查磁盘空间，不够则报 `ResourceLimitError`（`-32003`，可重试）并发 `disk_full_alarm` 事件。阈值 `disk_space_warning_pct`（默认 90）。

**引导式提示**：K8s 配置——`livenessProbe` 打 `/healthz`，`readinessProbe` 打 `/readyz`。两者别搞反：`/healthz` 永远 200，进程在就活，只能用来判断要不要重启进程；`/readyz` 看依赖，用来判断要不要接流量。Prometheus 抓 `/metrics`。日志用 RotatingFileHandler + JSON 格式，便于日志聚合。

### 11. 配置参考

配置优先级（高 → 低）：环境变量 > 用户配置文件 > `default_config.yaml`。

**用户配置文件**：`~/.fusion/artifacts/config.yaml`（或环境变量 `FUSION_ARTIFACTS_CONFIG` 指定路径）。

**环境变量**：
| 变量 | 说明 |
|------|------|
| `FUSION_ARTIFACTS_HOST` | 监听地址（默认 `127.0.0.1`） |
| `FUSION_ARTIFACTS_PORT` | 端口（默认 `11451`） |
| `FUSION_ARTIFACTS_STORAGE_ROOT` | 存储根目录（默认 `~/.fusion/artifacts`） |
| `FUSION_ARTIFACTS_API_KEY` | 鉴权密钥 |
| `FUSION_ARTIFACTS_NODE_ID` | 集群节点 ID |
| `FUSION_ARTIFACTS_CONFIG` | 配置文件路径覆盖 |

**`default_config.yaml` 关键段**（YAML 段名/键名即真实结构，直接抄进用户配置覆盖即可）：

```yaml
server:
  host: "127.0.0.1"
  port: 11451
  max_workers: 64                     # HTTP 线程池上限，0=不限

storage:
  root: "~/.fusion/artifacts"         # 存储根目录
  db_name: "meta.db"
  small_content_limit: 10240          # 小于此存 SQLite 内联，大于落盘
  sync_root: ""                       # artifact.sync 文件读写根，留空禁用 sync
  disk_space_warning_pct: 90          # 磁盘水位告警 %，0=禁用；写前预检

thresholds:
  auto_create_lines: 30               # render 阈值：行数
  auto_create_chars: 1500             # render 阈值：字符数

artifact:
  id_prefix: "art_"                   # ID 前缀
  max_versions_per_artifact: 100      # 单 artifact 版本上限，0=不限

security:
  api_key: "your-secret-key"          # 鉴权密钥；留空则读 FUSION_ARTIFACTS_API_KEY 环境变量
  allow_no_auth: false                # true 跳过鉴权（仅本地测试）
  recycle_retention_days: 7
  share_max_ttl_days: 90

sse:
  heartbeat_interval: 30

cluster:
  node_id: ""                         # 多节点 ID，单机留空

rate_limit:                           # 运维1: 令牌桶限流。rps=0 = 不限
  rps: 0
  burst: 0
  public_rps: 0                       # 公开 share 端点独立配额
  public_burst: 0

metrics:                              # 运维2: Prometheus /metrics 开关
  enabled: true
```

**引导式提示**：不要在代码里硬编码配置值，一律走 `ArtifactEngineConfig`。改配置后重启服务生效。生产环境务必设 `api_key`（或环境变量 `FUSION_ARTIFACTS_API_KEY`）并关掉 `allow_no_auth`。

### 12. 错误码与处理模式

JSON-RPC 错误码分层（错误体含 `code` / `message`，部分含 `retryable`）：

| code | 含义 | 可重试 | 典型场景 | 处理 |
|------|------|--------|----------|------|
| `-32001` | NotFound | 否 | artifact_id 不存在 | 检查 ID，可能已删除 |
| `-32002` | Conflict | 是 | 乐观锁哈希不匹配 | 重新取内容、拿新哈希再更新 |
| `-32003` | ResourceLimitError | 是 | 限流 / 磁盘满 | 退避后重试；磁盘满则扩容 |
| `-32004` | BusinessRuleError | 否 | 违反业务约束（如超版本上限） | 修正参数 |
| `-32005` | NotImplementedError | 否 | 调了已下线方法（inject/interact） | 改用替代方法 |

**HTTP 层**：
- JSON-RPC 成功：HTTP 200，body 含 `result`。
- JSON-RPC 业务错误：HTTP 200，body 含 `error`（注意不是 4xx/5xx，符合 JSON-RPC 规范）。
- 限流：HTTP 429，body 含错误（`-32003`，`retryable: true`）。
- REST 分享不存在：404；过期/超次数：410 Gone。
- 鉴权失败：401。
- `/metrics` 关闭：404。

**重试模式**：`retryable: true` 的错误（`-32002` Conflict、`-32003` 限流/磁盘）可退避重试。建议指数退避：先等 0.5s，翻倍到上限。`-32001` / `-32004` / `-32005` 不可重试，重试也没用，直接修正调用。

**引导式提示**：客户端看到 200 + `error` 别以为成功了——JSON-RPC 把业务错误放 200 body 里，要解析 `result` vs `error`。封装客户端时统一处理：有 `error` 抛异常，按 `retryable` 决定重试。

---

<!-- English part -->

## English Part

### 1. What This Is

`fusion-artifacts-engine` is a **structured artifact middleware** in the Fusion architecture. It **physically separates** large content generated in AI conversations (code, docs, HTML apps) from chat messages, storing each as an artifact while keeping only a lightweight reference (~30 tokens) in the conversation. Content is retrieved on demand when needed.

**Why you need it**: In long conversations iterating on 1000-line code, 10 rounds inflate context to ~120k tokens. Replacing with artifact references cuts that to ~15k for the same 10 rounds — ~87% context savings.

**Core capabilities**:
- Artifact CRUD + versioning (each update auto-creates a new version, history preserved)
- Auto render detection (HTML / React / SVG / Mermaid / Markdown)
- Public sharing (read-only links with expiry and access-count limits)
- Context budget and safety check
- Soft delete + recycle bin
- Ops probes (rate limiting, metrics, health/readiness)

**Position in Fusion**: Called by **fusion-studio** (GUI client, auto-starts this service on launch) and **fusion-code** (tool_use). Listens on `127.0.0.1:11451`, exposing three interfaces on the same port: JSON-RPC 2.0 (POST `/`), REST `/api/v1/*`, SSE `/api/v1/events/stream`.

### 2. Quick Start

**Prerequisite**: activate the repo-root venv (required before any Python work).

```bash
cd /Users/dahai/fusion
source .venv/bin/activate
cd fusion-artifacts-engine
pip install -e ".[all]"
```

**Start the daemon** (recommended; `start.sh` waits for health check before returning):

```bash
./start.sh start      # launches on 127.0.0.1:11451
./start.sh status     # exit 0 if healthy, 1 if not
./start.sh stop
./start.sh restart
```

Or use the CLI directly:

```bash
fusion-artifacts-engine start --host 127.0.0.1 --port 11451
fusion-artifacts-engine status
fusion-artifacts-engine version
```

**Verify the service is up** (send a ping):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

Expected: `{"jsonrpc":"2.0","id":1,"result":{"pong":true,"version":"0.4.1"}}`

**Auth note**: fail-closed by default (must send `X-API-Key`). Set env var `FUSION_ARTIFACTS_API_KEY=your-key`, then send the header on each request:

```bash
curl -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" ...
```

For local testing, set `allow_no_auth=true` (config file or code) to skip auth. `/healthz`, `/readyz`, `/metrics`, `/api/v1/share/*` require no auth.

**Your first artifact** (JSON-RPC create):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.create",
    "params":{
      "session_id":"demo-001",
      "name":"hello.py",
      "type":"code",
      "content":"def hello():\n    print(\\"hello fusion\\")\n",
      "summary":"example hello function"
    }
  }'
```

The `result.ref_text` is the ~30-token reference, looking like:

```
[Artifact: hello.py | ID: art_xxxx | Version: v1 | Type: code | Size: 42 | Summary: example hello function]
```

Put this `ref_text` in the conversation instead of the raw code. Retrieve content later with `artifact.get_content`.

### 3. Scenario 1: Store Long Code as an Artifact (Replace Big Content in Chat)

**Scenario**: AI generated an 800-line Python script. Pasting it into the chat eats a lot of context.

**What to do**: call `artifact.create`, get `ref_text`, keep only the reference in the conversation.

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.create",
    "params":{
      "session_id":"proj-2026-08",
      "name":"data_pipeline.py",
      "type":"code",
      "content":"<your 800 lines>",
      "summary":"ETL data cleaning pipeline",
      "kind":"code",
      "project_id":"proj-2026-08",
      "metadata":{"lang":"python","author":"dahai"}
    }
  }'
```

**Params**:
| Param | Required | Notes |
|-------|----------|-------|
| `session_id` | yes | session id, groups artifacts from one conversation |
| `name` | yes | artifact name (e.g. `data_pipeline.py`), drives type inference |
| `type` | yes | `code` / `markdown` / `html` / `react` / `data` |
| `content` | yes | full content |
| `summary` | no | short description (goes into ref_text, ≤200 chars) |
| `kind` | no | `app` / `code` / `document` / `game` / `tool` / `template`; auto-inferred if omitted |
| `project_id` | no | project id, groups artifacts across sessions |
| `metadata` | no | custom metadata (JSON object, filterable by field) |
| `change_log` | no | change note; auto-generated from diff if omitted |

**Returns**: `{artifact, version, ref_text}`. `artifact.id` looks like `art_a1b2c3` — all later operations use it.

**Key points**:
- Small content (<10KB, configurable) goes inline into SQLite; large content lands on disk at `content/{art_id}/v{num}.{ext}`.
- First version `change_log` defaults to `"Initial version"`.
- Retrieve content with `artifact.get_content`, pass `version` as `"latest"` or a version number.

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":2,"method":"artifact.get_content",
    "params":{"artifact_id":"art_a1b2c3","version":"latest"}
  }'
```

Returns: `{content, token_count, version}`.

**Guided tip**: If this is your first time, run the ping and first-artifact steps from section 2 first — confirm the service is up and auth works — then do this scenario.

### 4. Scenario 2: Version Iteration (Update, Diff, Rollback)

**Scenario**: AI revised that code in round 2, and again in round 3. Each round should become a new version, not an overwrite.

**Update content** (`artifact.update`, auto-creates v2):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.update",
    "params":{
      "artifact_id":"art_a1b2c3",
      "content":"<revised code>",
      "change_log":"add exception retry",
      "source":"ai_generation"
    }
  }'
```

**Params**:
| Param | Required | Notes |
|-------|----------|-------|
| `artifact_id` | yes | artifact id |
| `content` | yes | new full content |
| `change_log` | no | change note; auto-generated from diff if omitted |
| `source` | no | `manual` or `ai_generation`, records origin |
| `expected_content_hash` | no | optimistic lock: pass current content hash; mismatch → error (prevents lost updates from concurrent edits) |

Returns `{version, ref_text}`; `version.version_num` increments.

**Version diff** (see what changed between v2 and v5):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.version_diff",
    "params":{"artifact_id":"art_a1b2c3","from_version":2,"to_version":5}
  }'
```

Returns the diff. Pair with `artifact.version_list` to list all versions and `artifact.version_rollback` to roll back to a version.

**Rollback** (revert current version to v3; rollback creates a new version tagged as rollback):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.version_rollback",
    "params":{"artifact_id":"art_a1b2c3","target_version":3}
  }'
```

Returns `{version, ref_text}`; `version.version_num` is the new version number created by the rollback (not the target version itself — history is preserved).

**Guided tip**: The optimistic lock is useful when multiple clients edit the same artifact concurrently — pass the hash you got when fetching content; if someone changed it in between, the server returns Conflict (retryable), preventing your update from clobbering theirs.

### 5. Scenario 3: Auto Render Detection (render)

**Scenario**: AI output some HTML or a Mermaid diagram, and you want to decide whether it should become an artifact and whether the client can render it.

**What to do**: call `artifact.render`. It does four things — threshold check, type detection, store as artifact, return a `render_type` hint. It does **not** perform browser rendering (that's the client fusion-studio's job).

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.render",
    "params":{
      "content":"<!DOCTYPE html><html><body><h1>Hello</h1><p>'"$(python3 -c "print('"'"'lorem '"'"' * 400)")"'</p></body></html>",
      "session_id":"demo-001",
      "lang_hint":"html"
    }
  }'
```

> The example uses `python3 -c "print('lorem ' * 400)"` to pad content past 1500 chars, ensuring it crosses the threshold. Passing a short HTML snippet (e.g. a single `<h1>` line) returns `below_threshold` because it's under the limit.

**Returns**:
- Over threshold and renderable: `{created: true, artifact: {...}, render_type: "html", content, ref_text}`
- Too short (lines <30 and chars <1500): `{created: false, reason: "below_threshold"}`
- Empty: `{created: false, reason: "empty_content"}`
- Failure: `{created: false, reason: "<error>"}`

`render_type` may be: `html` / `react` / `svg` / `mermaid` / `markdown`. The client decides how to render based on this. Non-renderable → `render_type` is `null`.

**Thresholds** (configurable in `default_config.yaml`):
- Creates an artifact only if ≥30 lines or ≥1500 chars (below threshold, skipped).
- Renderable-type detection is content-based (e.g. `<!DOCTYPE html>` → html, `mermaid` keyword → mermaid).

**Guided tip**: `render` vs `create` — `create` is when you know you want to store; `render` is when you hand it content and let it decide whether to store and what type. When the client has AI output and isn't sure it should be an artifact, use `render`.

### 6. Scenario 4: Share an Artifact (share)

**Scenario**: Share an artifact as a read-only link, with expiry or an access-count cap.

**Create a share**:

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.create_share",
    "params":{
      "artifact_id":"art_a1b2c3",
      "created_by":"dahai",
      "expires_at":"2026-09-24T00:00:00Z",
      "max_accesses":100
    }
  }'
```

Returns `{share}`; `share.share_id` looks like `shr_xxxx`. The share link (REST, no auth):

```
http://127.0.0.1:11451/api/v1/share/shr_xxxx
```

Opening it in a browser shows the rendered read-only preview (server-side render, with a strict CSP header against XSS). Expired or over the access cap returns 410 Gone; nonexistent returns 404.

**Related ops**:
- `artifact.get_shared`: get share details by share ID.
- `artifact.revoke_share`: revoke a share (link becomes 404 after).

**Constraint**: max share TTL is bounded by `share_max_ttl_days` (default 90 days).

**Guided tip**: The share interface runs on a separate rate-limit bucket (`public_rate_limit_rps`), decoupled from the authed RPC limit, so public-link traffic can't drag down the normal API. In production, always set a sensible cap on the public bucket.

### 7. Scenario 5: Context Budget and Safety Check

**Scenario**: Before sending a request to the LLM, you want to know how much token budget the current conversation has left, or confirm content isn't over limit.

**Context budget** (`context.budget`):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"context.budget",
    "params":{"session_id":"demo-001","context_window":200000}
  }'
```

Returns current token usage, remaining budget, and whether it's safe.

**Safety check** (`artifact.check_safety`): pass a batch of messages and an output budget; judge whether it's over limit.

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.check_safety",
    "params":{"messages":[{"role":"user","content":"hello"}],"output_budget":10000}
  }'
```

Returns `{safe: true/false, current_tokens, remaining_tokens}`. `remaining_tokens < 0` means over limit.

**Auto-compact** (`artifact.auto_compact`): an artifact is too long; give it a token budget to compress.

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.auto_compact",
    "params":{"artifact_id":"art_a1b2c3","token_budget":4000}
  }'
```

**Guided tip**: `context.budget` checks the global budget, `check_safety` checks whether a specific batch of messages is safe, `auto_compact` proactively compresses one artifact when over limit. Use them together: budget to see the remainder → check_safety to validate what you're about to send → if over, auto_compact the artifact taking the most space.

**Note**: `artifact.inject` and `artifact.interact` were **removed** in v0.4.0 (ops batch 6); the RPC layer returns `-32005 NotImplementedError`. For safe context management, use `context.budget` + `auto_compact` instead.

### 8. Scenario 6: Lifecycle Management (Rename, Star, Pin, Duplicate)

**Scenario**: Artifacts pile up and need organizing — rename, star, pin, duplicate.

**Rename**:

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.rename",
    "params":{"artifact_id":"art_a1b2c3","new_name":"pipeline_v2.py"}
  }'
```

**Star / Pin**:

```bash
# Star
curl -s -X POST http://127.0.0.1:11451/ -H "Content-Type: application/json" -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.star","params":{"artifact_id":"art_a1b2c3"}}'

# Pin
curl -s -X POST http://127.0.0.1:11451/ -H "Content-Type: application/json" -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.pin","params":{"artifact_id":"art_a1b2c3"}}'
```

**Duplicate** (creates a new artifact with the same content, new ID):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.duplicate","params":{"artifact_id":"art_a1b2c3"}}'
```

**Organizing helpers**:
- `artifact.create_folder` / `list_folders` / `rename_folder` / `delete_folder` / `move_to_folder`: folder organization.
- `artifact.add_tag` / `remove_tag` / `list_tags` / `list_artifact_tags`: tags.
- `artifact.list`: filter by `session_id` / `project_id` / `metadata_filter`.
- `artifact.list_all`: list everything.
- `artifact.move_to_project_kb`: move an artifact into a project knowledge base.

**Guided tip**: `metadata` (the JSON you passed at create time) can be filtered by field via `metadata_filter` in `list` — e.g. list only `lang=python` artifacts. This is a more flexible organizing mechanism than folders, suited to custom classification.

### 9. Scenario 7: Recycle Bin (Soft Delete, Restore, Purge)

**Scenario**: Delete an artifact but not really — send it to the recycle bin first, recoverable if you regret it.

**Soft delete** (default; `soft_delete` defaults to `true`):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.delete",
    "params":{"artifact_id":"art_a1b2c3","soft_delete":true}
  }'
```

Returns `{ok: true}`. After a soft delete, `artifact.get` won't find it by default, but `artifact.list_recycle` lists items in the recycle bin.

**Restore**:

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.restore","params":{"artifact_id":"art_a1b2c3"}}'
```

**Purge** (auto-cleans recycle-bin items past the retention window):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.purge_expired","params":{}}'
```

**Hard delete** (delete directly, no recycle bin):

```bash
curl -s -X POST http://127.0.0.1:11451/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $FUSION_ARTIFACTS_API_KEY" \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"artifact.delete",
    "params":{"artifact_id":"art_a1b2c3","soft_delete":false}
  }'
```

**Constraint**: recycle-bin retention is controlled by `recycle_retention_days` (default 7 days). Expired items are cleaned by `purge_expired`.

**Guided tip**: Unless you're sure you don't want it, always soft-delete (the default). `purge_expired` is the means to periodically clean the recycle bin — in production, schedule a cron to call it. Hard delete is irreversible; use with care.

### 10. Scenario 8: Ops Probes (Health, Metrics, Readiness)

**Scenario**: After production deployment, Kubernetes / load balancers need to probe service state, and Prometheus scrapes metrics.

**Liveness probe** (`/healthz`, no auth, always 200):

```bash
curl -s http://127.0.0.1:11451/healthz
# {"status":"ok","check":"liveness"}
```

Returns 200 as long as the process is running — used to judge process liveness.

**Readiness probe** (`/readyz`, no auth, 200 only when dependencies are ready, else 503):

```bash
curl -s http://127.0.0.1:11451/readyz
# ready:     {"status":"ready","checks":{"storage":true,"event_bus":true}}
# not ready: {"status":"not_ready","checks":{"storage":false,...}}  (HTTP 503)
```

Checks `storage.health_check()` and `event_bus` state. If storage or the event bus is down, returns 503 and the load balancer should drain traffic.

**Metrics** (`/metrics`, no auth, Prometheus text format):

```bash
curl -s http://127.0.0.1:11451/metrics
```

Output includes `rpc_requests_total` (counter), `rpc_active_conns` (gauge), etc. With `metrics_enabled=false` returns 404.

**Rate limiting** (v0.4.0 ops batch 1): token-bucket algorithm. Over-limit returns HTTP 429; the JSON-RPC error body is `-32003` with `retryable: true`.

```bash
# over-limit response example
{"jsonrpc":"2.0","id":1,"error":{"code":-32003,"message":"rate limit exceeded","retryable":true}}
```

Two buckets:
- Default bucket (authed RPC): `rate_limit_rps` / `rate_limit_burst`; `rps=0` means unlimited.
- Public bucket (share interface): `public_rate_limit_rps` / `public_rate_limit_burst`.

**Disk check** (ops batch 4): before writing content, disk space is checked; if insufficient, raises `ResourceLimitError` (`-32003`, retryable) and emits a `disk_full_alarm` event. Threshold `disk_space_warning_pct` (default 90).

**Guided tip**: Kubernetes config — point `livenessProbe` at `/healthz` and `readinessProbe` at `/readyz`. Don't mix them up: `/healthz` is always 200 (process alive), only for deciding whether to restart the process; `/readyz` checks dependencies, for deciding whether to route traffic. Prometheus scrapes `/metrics`. Logs use RotatingFileHandler + JSON format for log aggregation.

### 11. Configuration Reference

Config precedence (high → low): env vars > user config file > `default_config.yaml`.

**User config file**: `~/.fusion/artifacts/config.yaml` (or path via env var `FUSION_ARTIFACTS_CONFIG`).

**Environment variables**:
| Var | Notes |
|-----|-------|
| `FUSION_ARTIFACTS_HOST` | listen address (default `127.0.0.1`) |
| `FUSION_ARTIFACTS_PORT` | port (default `11451`) |
| `FUSION_ARTIFACTS_STORAGE_ROOT` | storage root (default `~/.fusion/artifacts`) |
| `FUSION_ARTIFACTS_API_KEY` | auth key |
| `FUSION_ARTIFACTS_NODE_ID` | cluster node id |
| `FUSION_ARTIFACTS_CONFIG` | config file path override |

**Key sections of `default_config.yaml`** (YAML section/key names are the real structure — copy into your user config to override):

```yaml
server:
  host: "127.0.0.1"
  port: 11451
  max_workers: 64                     # HTTP thread-pool cap, 0=unlimited

storage:
  root: "~/.fusion/artifacts"         # storage root
  db_name: "meta.db"
  small_content_limit: 10240          # below this stored inline in SQLite, above on disk
  sync_root: ""                       # artifact.sync file root, empty disables sync
  disk_space_warning_pct: 90          # disk-watermark alarm %, 0=disabled; pre-write check

thresholds:
  auto_create_lines: 30               # render threshold: lines
  auto_create_chars: 1500             # render threshold: chars

artifact:
  id_prefix: "art_"                   # ID prefix
  max_versions_per_artifact: 100      # per-artifact version cap, 0=unlimited

security:
  api_key: "your-secret-key"          # auth key; empty → reads FUSION_ARTIFACTS_API_KEY env var
  allow_no_auth: false                # true skips auth (local testing only)
  recycle_retention_days: 7
  share_max_ttl_days: 90

sse:
  heartbeat_interval: 30

cluster:
  node_id: ""                         # multi-node id; empty for single machine

rate_limit:                           # ops-1: token-bucket rate limiting. rps=0 = unlimited
  rps: 0
  burst: 0
  public_rps: 0                       # separate quota for the public share endpoint
  public_burst: 0

metrics:                              # ops-2: Prometheus /metrics toggle
  enabled: true
```

**Guided tip**: Don't hardcode config values in code — always go through `ArtifactEngineConfig`. Restart the service for config changes to take effect. In production, always set `api_key` (or env var `FUSION_ARTIFACTS_API_KEY`) and turn off `allow_no_auth`.

### 12. Error Codes and Handling Patterns

JSON-RPC error codes are layered (error body has `code` / `message`, some have `retryable`):

| code | Meaning | Retryable | Typical case | Handling |
|------|---------|-----------|--------------|----------|
| `-32001` | NotFound | no | artifact_id doesn't exist | check the ID; may be deleted |
| `-32002` | Conflict | yes | optimistic-lock hash mismatch | re-fetch content, get the new hash, update again |
| `-32003` | ResourceLimitError | yes | rate limit / disk full | back off and retry; if disk full, expand capacity |
| `-32004` | BusinessRuleError | no | business constraint violated (e.g. version cap) | fix the params |
| `-32005` | NotImplementedError | no | called a removed method (inject/interact) | switch to the replacement |

**HTTP layer**:
- JSON-RPC success: HTTP 200, body has `result`.
- JSON-RPC business error: HTTP 200, body has `error` (note: not 4xx/5xx — per JSON-RPC spec).
- Rate limit: HTTP 429, body has the error (`-32003`, `retryable: true`).
- REST share nonexistent: 404; expired / over cap: 410 Gone.
- Auth failure: 401.
- `/metrics` disabled: 404.

**Retry pattern**: errors with `retryable: true` (`-32002` Conflict, `-32003` rate-limit/disk) can be retried with backoff. Suggested exponential backoff: start at 0.5s, double up to a cap. `-32001` / `-32004` / `-32005` are not retryable — retrying won't help; just fix the call.

**Guided tip**: Don't treat a 200 + `error` as success — JSON-RPC puts business errors in a 200 body; you must parse `result` vs `error`. When wrapping a client, handle it uniformly: on `error` raise an exception, and decide retry by `retryable`.

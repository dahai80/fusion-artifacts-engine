# Fusion Artifacts Engine

English | **[中文](./README_CN.md)**

Structured artifact CRUD middleware for the Fusion architecture. Physically separates generated artifacts from chat messages to solve context overflow in long AI conversations.

## How It Works

When a model generates long content (code, documents, HTML apps), the engine:
1. Stores the full content independently
2. Replaces it in the conversation with a lightweight reference (~30 tokens)
3. Returns content on-demand only when the model needs it

Result: **10 rounds of 1000-line code iteration uses ~15k tokens instead of ~120k**.

## Quick Start

```bash
# Install
pip install -e ".[all]"

# Start daemon
fusion-artifacts-engine start --port 11451

# Check status (exit 0 = healthy, 1 = not running)
fusion-artifacts-engine status
```

## Security (v0.3.11 audit hardening)

**v0.3.11** patches the `start.sh` lifecycle script: `ensure_venv` now prefers the repo-root `.venv` (monorepo convention) over a stale project-local `.venv`, fixing `ping.version` reporting an outdated version. No engine change; 385 tests green, ruff clean.

v0.3.8 resolved all 59 findings from the 0824 security audit. v0.3.9 addressed the runtime/engineering findings (R1-R9, E1-E11) plus H2/H3/H6. **v0.3.10 completes the architecture refactors H5 and H7**, the two that materially change runtime behavior. H1 and H8 are retained by design (see below). 385 tests green, ruff clean. v0.3.10 highlights:

- **H5 event-loop thread offload**: blocking synchronous storage calls inside async engine methods now run via `asyncio.to_thread` (default ThreadPoolExecutor), so sqlite/file I/O no longer stalls the single event loop while a write holds the write-lock. Storage stays thread-safe (`check_same_thread=False` + `_write_lock` + read pool of 4).
- **H7 engine split**: the 1709-line `engine.py` god-class is split into modules behind a thin delegation layer (behavior-preserving) — `share.py` (`ShareManager` class with the R2 access buffer), `token_budget.py` (`context_budget`/`check_safety`/`inject`), and `section_index.py` (extended with `all_section_bounds`/`build_sections_with_tokens`/`replace_section`/`delete_section`). `engine.py` is now ~1300 lines and delegates.

### H1 / H8 — retained by design

- **H1 write-connection pool**: not added. SQLite WAL with `BEGIN IMMEDIATE` physically serializes writes, so a write-connection pool would be cargo-cult — the real write-amplification pain (file I/O under lock) was fixed by H2's two-phase write. A single write connection is architecturally correct.
- **H8 multi-node storage**: the `StorageDriver` ABC (46 abstract methods) is the swap point; actual multi-node replication belongs to the `fusion-multi-node` project, out of scope for this middleware.

### v0.3.9 retained highlights:

- **R9 version-limit eviction**: `max_versions_per_artifact` enforced — evicts oldest non-snapshot versions past the limit (default 100, 0=unlimited). Fixed a transaction-leak regression where eviction DELETE left an implicit txn open.
- **R2 share write amplification**: public share access is memory-buffered (5s/50-count flush), no write-lock per GET; `max_accesses` returns `410 Gone` once exhausted.
- **H2 two-phase file write**: large content writes go to `.tmp_*` outside the transaction, committed then renamed; `gc_orphan_files()` cleans orphans + residue on rollback.
- **R1 thread cap**: `server_max_workers` (BoundedSemaphore, default 64) — over-cap connections get `503`, no unbounded thread spawn.
- **R8 error codes**: JSON-RPC custom range `-32001` NotFound / `-32002` Conflict (retryable) / `-32003` ResourceLimit (retryable) / `-32004` BusinessRule, mapped to REST HTTP codes.
- **H5 timeout tiers**: per-method timeout (ping 5s, render/auto_compact 120s, default 30s); large-body (≥2MB) auto-escalates to 120s.
- **H6 token cache**: LRU `count_tokens` cache (blake2b-keyed, max 2048, skip <256B).
- **H3 share render**: `lxml` Cleaner (real HTML parser) replaces regex sanitization; strict CSP on share iframe.
- **E7 read connection pool**: read-only connection pool (max 4) replaces per-call connect/close.
- **R5 migration gating**: `applied_migrations` registry — migrations run once, idempotent on restart.
- **E9 lint gate**: `[tool.ruff]` config; `ruff check .` clean.
- **E11 concurrency/fault tests**: cover H2 orphan GC, R9 eviction, R2 buffer, H6 cache, R8 codes, H8 ABC contract, R1 thread cap, E7 pool reuse, R5 gating, plus H5 to_thread offload and H7 module extraction/line-count.

v0.3.8 retained highlights:

- **Auth fail-closed**: `allow_no_auth` defaults `false`; no configured key rejects requests (was fail-open).
- **Path confinement**: `sync_root` bounds file sync; content paths validated `is_relative_to`; extension whitelist.
- **Atomic versioning**: `create_version_atomic` (BEGIN IMMEDIATE) closes the optimistic-lock race.
- **EventBus**: per-engine instance (no cross-engine bleed), subscriber cap, drop/closed SSE signals.

See `audit/fusion-artifacts-audit-0824.md` §9 for the per-finding fix map.

## Authentication

The engine uses API key authentication via the `X-API-Key` header.

- If `api_key` is configured (env `FUSION_ARTIFACTS_API_KEY` or `security.api_key`), all requests must include `X-API-Key: <key>` (constant-time compare)
- If `api_key` is **not** configured, requests are **rejected by default** (`allow_no_auth: false`, fail-closed)
- Set `allow_no_auth: true` only for trusted single-user local use; production should set `api_key`

```yaml
# default_config.yaml
security:
  api_key: ""
  allow_no_auth: false
  recycle_retention_days: 7
  share_max_ttl_days: 90
```

## JSON-RPC API

The engine exposes an HTTP JSON-RPC 2.0 server. Example:

```bash
curl -X POST http://127.0.0.1:11451 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"artifact.create","params":{"session_id":"sess_1","name":"hello.py","type":"code","content":"print(\"hello\")"},"id":1}'
```

### Core Methods

| Method | Params | Description |
|---|---|---|
| `artifact.create` | session_id, name, type, content, summary?, kind?, project_id?, metadata?, owner_user_id?, ownership_type? | Create artifact + v1 |
| `artifact.get` | artifact_id, project_id? | Get metadata |
| `artifact.get_content` | artifact_id, version? | Get version content |
| `artifact.list` | session_id, include_deleted?, project_id?, metadata_filter? | List session artifacts |
| `artifact.delete` | artifact_id, soft_delete?, project_id? | Soft/hard delete artifact |
| `artifact.update` | artifact_id, content, change_log?, source?, expected_content_hash? | Create new version (optimistic lock) |
| `artifact.patch` | artifact_id, operation, anchor?, content?, expected_version? | Patch artifact (replace_section/append/prepend/delete_section) |
| `artifact.load` | artifact_id, preview_only?, section? | Load artifact (preview/section/full, per-section token counts) |
| `context.budget` | session_id, context_window? | Session token budget summary |
| `artifact.auto_compact` | artifact_id, token_budget | Auto-compress artifact to fit token budget; returns `reason` on no-op (`already_within_budget`/`could_not_reduce`) |
| `artifact.version_diff` | artifact_id, from_version, to_version | Unified diff between two versions |
| `artifact.render` | content, session_id, lang_hint, project_id? | Auto-detect type, create renderable artifact |
| `artifact.check_safety` | messages, output_budget | Token budget safety check |
| `artifact.inject` | messages, output_budget | Token accounting check; returns `injected` + `note` (no-op: messages unchanged) |
| `artifact.interact` | artifact_id, action, payload, session_id? | Record interaction event; returns `dispatched` + `note` (stub: no action dispatch) |
| `artifact.sync` | artifact_id, file_path, direction | Sync artifact content ↔ file |
| `artifact.version_list` | artifact_id, page?, page_size?, include_content? | List versions (paginated, default page_size=200, cap 500) |
| `artifact.version_rollback` | artifact_id, target_version | Rollback to version |
| `artifact.export` | artifact_id, include_versions? | Export artifact data |
| `artifact.export_session` | session_id, output_dir | Batch export session |
| `artifact.import` | session_id, data | Import artifact |
| `artifact.export_code` | artifact_id, language? | Export as source code |
| `artifact.import_code` | session_id, code, language?, name?, metadata? | Create artifact from code |
| `artifact.watch` | artifact_id, action, watcher_id?, since_version? | Watch for changes |
| `ping` | — | Health check |

### Lifecycle Methods (P1)

| Method | Params | Description |
|---|---|---|
| `artifact.rename` | artifact_id, name | Rename artifact |
| `artifact.star` | artifact_id, starred | Star/unstar artifact |
| `artifact.pin` | artifact_id, pinned, chat_id? | Pin/unpin artifact to chat |
| `artifact.duplicate` | artifact_id | Duplicate artifact with new ID |
| `artifact.list_all` | owner_user_id?, ownership_type?, folder_id?, is_starred? | List all artifacts (cross-session) |

### Recycle Bin Methods (P1)

| Method | Params | Description |
|---|---|---|
| `artifact.list_recycle` | — | List soft-deleted artifacts |
| `artifact.restore` | artifact_id | Restore from recycle bin |
| `artifact.purge_expired` | — | Hard-delete expired recycled items |

### Share Methods (P1)

| Method | Params | Description |
|---|---|---|
| `artifact.create_share` | artifact_id, max_accesses?, expires_at? | Create share link |
| `artifact.get_shared` | share_id | Get shared artifact (public) |
| `artifact.revoke_share` | artifact_id | Revoke share link |

### Snapshot Methods (P2)

| Method | Params | Description |
|---|---|---|
| `artifact.create_snapshot` | artifact_id, label? | Create named snapshot of current content |
| `artifact.list_snapshots` | artifact_id | List snapshots |

### Folder Methods (P2)

| Method | Params | Description |
|---|---|---|
| `artifact.create_folder` | name, parent_id? | Create folder |
| `artifact.list_folders` | — | List all folders |
| `artifact.rename_folder` | folder_id, name | Rename folder |
| `artifact.delete_folder` | folder_id | Delete folder |
| `artifact.move_to_folder` | artifact_id, folder_id | Move artifact to folder |

### Tag Methods (P4)

| Method | Params | Description |
|---|---|---|
| `artifact.add_tag` | artifact_id, tag_name | Add tag (creates if needed) |
| `artifact.remove_tag` | artifact_id, tag_name | Remove tag from artifact |
| `artifact.list_tags` | — | List all tags |
| `artifact.list_artifact_tags` | artifact_id | List tags on artifact |

### Event Methods (P4)

| Method | Params | Description |
|---|---|---|
| `artifact.emit_event` | artifact_id, event_type, payload? | Emit event |
| `artifact.list_events` | artifact_id?, event_type?, limit? | List events |

### Project KB Method (P3)

| Method | Params | Description |
|---|---|---|
| `artifact.move_to_project_kb` | artifact_id | Move artifact to project knowledge base |

### External Module Methods

| Method | Params | Description |
|---|---|---|
| `artifact.create_external` | source_module, workspace_id, name, type, content, workflow_run_id?, summary?, kind?, project_id?, metadata? | Create artifact from external module (e.g. fusion-mlx) |
| `artifact.list_by_source` | source_module, workspace_id?, workflow_run_id? | List artifacts by source module |

### Artifact Kinds

Semantic classification matching how users think about artifacts:

| Kind | Description |
|---|---|
| `app` | Interactive web apps, dashboards, websites |
| `code` | Code snippets, algorithms, scripts |
| `document` | Structured documents, reports, templates |
| `game` | Playable games, simulations, interactive challenges |
| `tool` | Productivity utilities, calculators, planners |
| `template` | Creative projects, quizzes, reusable patterns |

When `kind` is not specified, it is auto-inferred from `type`:
- `html` / `react` → `app`
- `markdown` → `document`
- `code` → `code`
- `data` → `tool`

### Version Source Tracking

Each version records its `source`:
- `manual` — user-initiated edit
- `ai_generation` — AI-driven update

When `change_log` is omitted, it is auto-generated from the content diff (e.g., "+5 lines, -2 lines").

### Optimistic Locking

The `artifact.update` method supports an optional `expected_content_hash` parameter. When provided, the update will fail with a hash mismatch error if the current content hash doesn't match, preventing lost updates in concurrent scenarios.

```bash
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.update","params":{"artifact_id":"art_xxx","content":"...","expected_content_hash":"sha256:abc123"}}'
```

### Code-Artifact Sync

**Export code** — get artifact content as source code:
```bash
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.export_code","params":{"artifact_id":"art_xxx","language":"python"}}'
```

**Import code** — create artifact from code:
```bash
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.import_code","params":{"session_id":"s1","code":"def foo(): pass","language":"python"}}'
```

**Watch** — poll for changes since a version:
```bash
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.watch","params":{"artifact_id":"art_xxx","action":"poll","since_version":3}}'
```

### Project Scope

Artifacts can be scoped to a `project_id` for multi-project isolation:

```bash
# Create with project scope
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.create","params":{"session_id":"s1","name":"app.py","type":"code","content":"...","project_id":"my-project"}}'

# List artifacts in a project
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.list","params":{"session_id":"s1","project_id":"my-project"}}'
```

### Metadata

Artifacts support arbitrary JSON metadata for filtering and classification:

```bash
# Create with metadata
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.create","params":{"session_id":"s1","name":"Button.tsx","type":"react","content":"...","metadata":{"framework":"react","component_name":"Button"}}}'

# List artifacts filtered by metadata
curl -X POST http://127.0.0.1:11451 \
  -d '{"method":"artifact.list","params":{"session_id":"s1","metadata_filter":{"framework":"react"}}}'
```

Metadata is stored as JSON in SQLite and queried via `json_extract()`. You can combine `metadata_filter` with `project_id` for scoped queries.

## REST API

### REST /api/v1 (P3)

RESTful CRUD API alongside JSON-RPC:

**GET** endpoints:
```bash
GET /api/v1/artifacts?session_id=sess-001            # List artifacts in a session (also: include_deleted, project_id, metadata_filter, filters)
GET /api/v1/artifacts?page=1&page_size=20&sort=updated_at  # List all artifacts (no session_id); supports filters JSON
GET /api/v1/artifacts/{artifact_id}                  # Get artifact metadata
GET /api/v1/artifacts/{artifact_id}/versions         # List artifact versions
GET /api/v1/artifacts/{artifact_id}/versions/{num}   # Get a specific version's content
GET /api/v1/share/{share_id}                         # Public share access (no auth; 410 Gone if revoked/expired/exhausted)
```

Query parameters for `GET /api/v1/artifacts`:
- `session_id` — scope list to a session; omitted → list all artifacts (paginated)
- `include_deleted` — include recycle-bin artifacts (true/false)
- `project_id` — scope to a project KB
- `metadata_filter` / `filters` — JSON query object passed to storage filtering
- `page` / `page_size` — pagination (list-all mode)
- `sort` — sort field (updated_at, created_at, name, starred)

**POST** endpoints:
```bash
POST /api/v1/artifacts/create    # {"session_id": "...", "name": "...", "artifact_type": "code", "content": "..."}  → 201
POST /api/v1/artifacts/{id}      # {"action": "delete"} → delete; otherwise → artifact.update
```

All other mutations (rename, star, pin, duplicate, restore, move-to-kb, move-to-folder,
snapshot, share creation, tags, folders, events, purge, external-source create) are
**JSON-RPC only** — the REST surface intentionally covers artifact CRUD + public share;
see the method table above for the JSON-RPC method names (e.g. `artifact.create_share`).

### SSE Events Stream (P4)

Server-Sent Events endpoint for real-time push-based artifact change notifications:
```bash
curl -N http://127.0.0.1:11451/api/v1/events/stream
```
Returns `text/event-stream` with heartbeat every 30 seconds (configurable via `sse.heartbeat_interval`).

Events are pushed in real-time via an internal EventBus — no polling required. Supported event types: `artifact.created`, `artifact.updated`, `artifact.deleted`.

Filter by artifact kind:
```bash
curl -N "http://127.0.0.1:11451/api/v1/events/stream?kind=app"
```

## Python SDK

```python
import asyncio
from fusion_artifacts_engine import ArtifactEngine, ArtifactEngineConfig

engine = ArtifactEngine()


async def main():
    # Create with kind
    art, ver, ref = await engine.create_artifact(
        "sess_1",
        "app.py",
        "code",
        "print('hello')\n" * 50,
        summary="Main application",
        kind="tool",
    )
    print(f"Created: {art.id} kind={art.kind}")

    # Update with source tracking
    v2, ref2 = await engine.create_version(
        art.id, "print('world')\n" * 60, change_log="", source="ai_generation"
    )

    # Export as code
    code_data = engine.export_code(art.id, "python")

    # Import from code
    new_art, _, _ = await engine.import_code(
        "s1", "def bar(): pass", language="python", name="bar.py"
    )

    # Lifecycle operations
    await engine.rename_artifact(art.id, "new_name.py")
    await engine.star_artifact(art.id, True)
    await engine.duplicate_artifact(art.id)

    # Share
    share = await engine.create_share(art.id, max_accesses=10)

    # Folders & tags
    folder = await engine.create_folder("My Folder")
    await engine.move_to_folder(art.id, folder.id)
    await engine.add_tag(art.id, "important")

    engine.close()


asyncio.run(main())
```

## Supported Artifact Types

| Type | Description |
|---|---|
| `code` | Python, JS, Rust, etc. |
| `markdown` | Documents, READMEs |
| `html` | Web pages, dashboards |
| `react` | React/JSX components |
| `data` | JSON, CSV, YAML |

## Storage

- **Metadata**: SQLite at `~/.fusion/artifacts/meta.db`
- **Content**: `~/.fusion/artifacts/content/{art_id}/v{num}.{ext}`
- Small content (<10KB) stored inline in SQLite
- Large content stored on filesystem

### Data Model

**Artifact** — core entity with ownership, lifecycle, and organization fields:
- Ownership: `owner_user_id`, `ownership_type` (personal/team/project)
- Lifecycle: `is_deleted`, `deleted_at` (soft delete), `is_starred`, `is_pinned`, `pinned_chat_id`
- Organization: `folder_id`, `share_id`, `in_project_kb`, `content_hash`, `active_in_session`
- External source: `source_module`, `workspace_id`, `workflow_run_id`

**ArtifactVersion** — versioned content with snapshot support:
- Snapshot: `snapshot_type` (auto/named/rollback), `snapshot_label`, `author`, `parent_version` (rollback versions tag the restored version, preserving history)
- Size: `size_bytes` (byte length of content)
- Tokens: `token_count` (tiktoken cl100k_base token count)
- Index: `section_index` (JSON array of {anchor, level} for section navigation)

**ArtifactShare** — share links with access control:
- `share_id` (shr_*), `max_accesses`, `access_count`, `expires_at` (ISO datetime; expiry compared via timezone-aware parse, not string compare), `is_revoked`
- Access control enforced on public GET: returns `410 Gone` once `access_count` (persisted + in-memory buffer) reaches `max_accesses`. Public access is buffered in memory and flushed every 5s / 50 counts to avoid write-lock contention (R2); `max_accesses=None` means unlimited.

**ArtifactFolder** — hierarchical artifact organization

**ArtifactTag** — tagging with many-to-many via `artifact_tag_map`

**ArtifactEvent** — audit trail for artifact changes

## Security

- **Fail-closed auth**: rejects requests when no API key is configured unless `allow_no_auth=True`
- **Optimistic locking**: concurrent update detection via `expected_content_hash`
- **Path traversal protection**: export paths are sanitized

## Configuration

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
  allow_no_auth: false   # fail-closed default; set true only for trusted single-user local use
  recycle_retention_days: 7

sse:
  heartbeat_interval: 30
```

## Architecture

```
Product Layer: Fusion Code (tool_use) | Fusion Studio (GUI)
---------------------------------------------------------
Middleware: Artifacts Engine (Python daemon, JSON-RPC 2.0 + REST /api/v1 + SSE)
  engine.py (domain core, ~1300 lines, delegates to modules)
    ├─ share.py        ShareManager — share access control + R2 access buffer
    ├─ token_budget.py context_budget / check_safety / inject
    ├─ section_index.py section bounds + patch (replace/delete section)
    ├─ render.py       share HTML render (lxml Cleaner)
    └─ auto_identifier.py should_create_artifact (threshold + renderable-type)
  async engine methods offload blocking storage calls via asyncio.to_thread
---------------------------------------------------------
Storage: SQLite (WAL, single write conn + read pool of 4) + Filesystem
  StorageDriver ABC (46 abstract methods) — swap point for multi-node/Postgres
```

## Roadmap

The refactoring blueprint to make fusion-artifacts-engine competitive with Claude Artifacts (AR = Architecture Refactor) covers:

- Competitive gap matrix vs Claude Artifacts
- Data model expansion, new engine ops (rename/star/pin/duplicate/snapshot/share/recycle/migrate-KB), SSE event bus, REST `/api/v1` parity, optimistic locking
- 4-phase implementation plan (P1-P4) — **all phases implemented**

## Running Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

### Test Coverage

```bash
pytest tests/ --cov=fusion_artifacts_engine --cov-report=term-missing
```

Current coverage: **92%** across 385 tests.

## License

Apache License 2.0

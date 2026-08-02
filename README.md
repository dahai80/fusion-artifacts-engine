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

# Check status
fusion-artifacts-engine status
```

## Authentication

The engine uses API key authentication via the `X-API-Key` header.

- If `api_key` is configured, all requests must include `X-API-Key: <key>`
- If `api_key` is **not** configured, requests are **allowed by default** (`allow_no_auth: true`)
- Set `api_key` and `allow_no_auth: false` to enforce auth in production

```yaml
# default_config.yaml
security:
  api_key: ""
  allow_no_auth: true
  recycle_retention_days: 7
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
| `artifact.version_list` | artifact_id | List all versions |
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
GET /api/v1/artifacts          # List artifacts (supports query params: created_by, since, until, kind, type, sort, page, page_size)
GET /api/v1/external?source_module=fusion-mlx&workspace_id=ws-001  # List artifacts by source module
GET /api/v1/folders            # List folders
GET /api/v1/tags               # List tags
GET /api/v1/events             # List events
GET /api/v1/recycle            # List recycle bin
```

Query parameters for `GET /api/v1/artifacts`:
- `created_by` — filter by owner_user_id
- `since` / `until` — filter by created_at timestamp (Unix epoch)
- `kind` — filter by artifact kind (app/code/document/game/tool/template)
- `type` — filter by artifact type (code/markdown/html/react/data)
- `sort` — sort field (updated_at, created_at, name, starred)
- `page` / `page_size` — pagination

**POST** endpoints (action-based):
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
        "sess_1", "app.py", "code", "print('hello')\n" * 50,
        summary="Main application", kind="tool"
    )
    print(f"Created: {art.id} kind={art.kind}")

    # Update with source tracking
    v2, ref2 = await engine.create_version(
        art.id, "print('world')\n" * 60,
        change_log="", source="ai_generation"
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
- Snapshot: `snapshot_type` (auto/named), `snapshot_label`, `author`, `parent_version`
- Size: `size_bytes` (byte length of content, replaces former `token_count`)

**ArtifactShare** — share links with access control:
- `share_id` (shr_*), `max_accesses`, `access_count`, `expires_at`, `is_revoked`

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
  allow_no_auth: true
  recycle_retention_days: 7

sse:
  heartbeat_interval: 30
```

## Architecture

```
Product Layer: Fusion Code (tool_use) | Fusion Studio (GUI)
---------------------------------------------------------
Middleware: Artifacts Engine (Python daemon, JSON-RPC 2.0 + REST /api/v1 + SSE)
---------------------------------------------------------
Storage: SQLite (WAL) + Filesystem
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

Current coverage: **92%** across 278+ tests.

## License

Apache License 2.0

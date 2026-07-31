# Fusion Artifacts Engine

Artifact management middleware for the Fusion architecture. Physically separates generated artifacts from chat messages to solve context overflow in long AI conversations.

## How It Works

When a model generates long content (code, documents, HTML apps), the engine:
1. Stores the full content independently
2. Replaces it in the conversation with a lightweight reference (~30 tokens)
3. Injects full content on-demand only when the model needs it

Result: **10 rounds of 1000-line code iteration uses ~15k tokens instead of ~120k**.

## Quick Start

```bash
# Install
pip install -e ".[all]"

# Start daemon
fusion-artifacts-engine start --port 8892

# Check status
fusion-artifacts-engine status
```

## Authentication

The engine uses API key authentication via the `X-API-Key` header.

- If `api_key` is configured, all requests must include `X-API-Key: <key>`
- If `api_key` is **not** configured, requests are **rejected by default** (fail-closed)
- Set `allow_no_auth: true` in config to allow unauthenticated access (dev/test only)

```yaml
# default_config.yaml
security:
  api_key: ""           # Set to enable auth
  allow_no_auth: false  # Set true for dev/test without auth
```

## JSON-RPC API

The engine exposes an HTTP JSON-RPC 2.0 server. Example:

```bash
curl -X POST http://127.0.0.1:8892 \
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
| `artifact.inject` | messages, max_context? | Pre-request content injection |
| `artifact.check_safety` | messages, max_context? | Token safety check |
| `artifact.export` | artifact_id, include_versions? | Export artifact data |
| `artifact.export_session` | session_id, output_dir | Batch export session |
| `artifact.import` | session_id, data | Import artifact |
| `artifact.export_code` | artifact_id, language? | Export as source code |
| `artifact.import_code` | session_id, code, language?, name?, metadata? | Create artifact from code |
| `artifact.watch` | artifact_id, action, watcher_id?, since_version? | Watch for changes |
| `artifact.sync` | artifact_id, code_path, direction | Bidirectional code sync |
| `artifact.render` | session_id, content, type?, viewport?, project_id? | Detect renderable content, create artifact |
| `artifact.interact` | artifact_id, action, payload, session_id? | Canvas interaction → version update |
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
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.update","params":{"artifact_id":"art_xxx","content":"...","expected_content_hash":"sha256:abc123"}}'
```

### Code-Artifact Sync

**Export code** — get artifact content as source code:
```bash
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.export_code","params":{"artifact_id":"art_xxx","language":"python"}}'
```

**Import code** — create artifact from code:
```bash
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.import_code","params":{"session_id":"s1","code":"def foo(): pass","language":"python"}}'
```

**Watch** — poll for changes since a version:
```bash
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.watch","params":{"artifact_id":"art_xxx","action":"poll","since_version":3}}'
```

**Sync** — bidirectional file sync:
```bash
# Artifact → file
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.sync","params":{"artifact_id":"art_xxx","code_path":"/path/to/file.py","direction":"artifact_to_code"}}'

# File → artifact
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.sync","params":{"artifact_id":"art_xxx","code_path":"/path/to/file.py","direction":"code_to_artifact"}}'
```

### Project Scope

Artifacts can be scoped to a `project_id` for multi-project isolation:

```bash
# Create with project scope
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.create","params":{"session_id":"s1","name":"app.py","type":"code","content":"...","project_id":"my-project"}}'

# List artifacts in a project
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.list","params":{"session_id":"s1","project_id":"my-project"}}'
```

### Metadata

Artifacts support arbitrary JSON metadata for filtering and classification:

```bash
# Create with metadata
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.create","params":{"session_id":"s1","name":"Button.tsx","type":"react","content":"...","metadata":{"framework":"react","component_name":"Button"}}}'

# List artifacts filtered by metadata
curl -X POST http://127.0.0.1:8892 \
  -d '{"method":"artifact.list","params":{"session_id":"s1","metadata_filter":{"framework":"react"}}}'
```

Metadata is stored as JSON in SQLite and queried via `json_extract()`. You can combine `metadata_filter` with `project_id` for scoped queries.

## REST API

### Render API

REST endpoints for fusion-studio canvas integration. The engine detects renderable content types (HTML, SVG, Mermaid, React) and returns a URL for iframe embedding.

**Render** — detect + create artifact for canvas:
```bash
curl -X POST http://127.0.0.1:8892/api/artifact/render \
  -H "Content-Type: application/json" \
  -d '{"content":"<!DOCTYPE html><html><body>Hello</body></html>","session_id":"s1","type":"auto","viewport":{"width":800,"height":600}}'
```
Response: `{"renderable": true, "artifact_id": "art_xxx", "artifact_type": "html", "render_url": "/api/artifact/content/art_xxx", "viewport": {"width": 800, "height": 600}}`

Supported `type` values: `"auto"` (detect), `"html"`, `"svg"`, `"mermaid"`, `"react"`. SVG content is wrapped in an HTML page; Mermaid content is wrapped with the Mermaid.js runtime.

**Get Content** — serve artifact content for iframe (auth required, CSP sandboxed):
```bash
curl http://127.0.0.1:8892/api/artifact/content/art_xxx
```
Returns raw content with `Content-Security-Policy` and `X-Content-Type-Options: nosniff` headers.

**Interact** — bidirectional canvas → chat context:
```bash
curl -X POST http://127.0.0.1:8892/api/artifact/interact \
  -H "Content-Type: application/json" \
  -d '{"artifact_id":"art_xxx","action":"user_edit","payload":{"content":"<!DOCTYPE html><html><body>Updated</body></html>"}}'
```
Actions: `user_click`, `user_edit`, `state_change`. Creates a new artifact version and returns a `ref_text` for chat context injection.

### Share API (P1)

Public endpoint for shared artifact access (no auth required):
```bash
curl http://127.0.0.1:8892/api/share/shr_xxx
```
Returns artifact content with CSP sandbox headers. Access is tracked and expired/revoked shares return 410 Gone.

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
curl -N http://127.0.0.1:8892/api/v1/events/stream
```
Returns `text/event-stream` with heartbeat every 30 seconds (configurable via `sse.heartbeat_interval`).

Events are pushed in real-time via an internal EventBus — no polling required. Supported event types: `artifact.created`, `artifact.updated`, `artifact.deleted`.

Filter by artifact kind:
```bash
curl -N "http://127.0.0.1:8892/api/v1/events/stream?kind=app"
```

### Token Count API

Standalone HTTP endpoint for token counting (outside JSON-RPC):

```bash
curl -X POST http://127.0.0.1:8892/api/token-count \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello, world!"}'
```

Response: `{"token_count": 4}`

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

    # Sync artifact to file
    await engine.sync_artifact_file(art.id, "/path/to/file.py", "artifact_to_code")

    # Inject for model context
    messages = [{"role": "assistant", "content": ref}]
    injected, total, safe = await engine.inject(messages)

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

## Tool Use Schemas

For integration with fusion-code, register these tool schemas:

```python
from fusion_artifacts_engine.tool_schemas.create_artifact import CREATE_ARTIFACT_SCHEMA
from fusion_artifacts_engine.tool_schemas.update_artifact import UPDATE_ARTIFACT_SCHEMA
```

When the model calls `create_artifact`, the engine returns a `[Artifact: ...]` reference that replaces the full content in chat history.

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

**ArtifactShare** — share links with access control:
- `share_id` (shr_*), `max_accesses`, `access_count`, `expires_at`, `is_revoked`

**ArtifactFolder** — hierarchical artifact organization

**ArtifactTag** — tagging with many-to-many via `artifact_tag_map`

**ArtifactEvent** — audit trail for artifact changes

## Security

- **Fail-closed auth**: rejects requests when no API key is configured unless `allow_no_auth=True`
- **CSP sandbox**: all artifact content responses include `Content-Security-Policy` header
- **X-Content-Type-Options**: `nosniff` on all content responses
- **IDOR protection**: artifact content endpoint requires authentication
- **Optimistic locking**: concurrent update detection via `expected_content_hash`

## Configuration

```yaml
# default_config.yaml
server:
  host: "127.0.0.1"
  port: 8892

storage:
  root: "~/.fusion/artifacts"

security:
  api_key: ""
  allow_no_auth: false
  recycle_retention_days: 7

sse:
  heartbeat_interval: 30
```

## Token Counting Strategy

Three-tier fallback:
1. fusion-mlx `/v1/messages/count_tokens` — most accurate
2. tiktoken (cl100k_base) — fast, good approximation
3. `len(text) // 4` — heuristic fallback

## Architecture

```
Product Layer: Fusion Code (tool_use) │ Fusion Studio (GUI)
─────────────────────────────────────────────────────────────
Middleware: Artifacts Engine (Python daemon, JSON-RPC 2.0 + REST /api/v1 + SSE)
─────────────────────────────────────────────────────────────
Storage: SQLite (WAL) + Filesystem
─────────────────────────────────────────────────────────────
Inference: fusion-mlx (local) │ Cloud APIs
```

## Roadmap

The refactoring blueprint to make fusion-artifacts-engine competitive with Claude Artifacts is documented in [fusion-artifact-enhance-ar.md](./fusion-artifact-enhance-ar.md) (AR = Architecture Refactor). It covers:

- Competitive gap matrix vs Claude Artifacts (追平 9 项 / 超越 5 项 / 二期 1 项)
- Data model expansion, new engine ops (rename/star/pin/duplicate/snapshot/share/recycle/migrate-KB), rendering hardening (CSP sandbox, Mermaid/Markdown localization), ```artifact fence parsing, SSE event bus, REST `/api/v1` parity, optimistic locking
- 4-phase implementation plan (P1-P4) — **all phases implemented**
- Cross-repo issue/PR proposals for fusion-studio / fusion-agent-studio / fusion-projects / fusion-mlx / fusion-cowork (GUI designs in md ASCII mockups)

## Running Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

## License

Apache License 2.0

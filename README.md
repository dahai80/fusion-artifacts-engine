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

## JSON-RPC API

The engine exposes an HTTP JSON-RPC 2.0 server. Example:

```bash
curl -X POST http://127.0.0.1:8892 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"artifact.create","params":{"session_id":"sess_1","name":"hello.py","type":"code","content":"print(\"hello\")"},"id":1}'
```

### Available Methods

| Method | Params | Description |
|---|---|---|
| `artifact.create` | session_id, name, type, content, summary?, kind?, project_id?, metadata? | Create artifact + v1 |
| `artifact.get` | artifact_id, project_id? | Get metadata |
| `artifact.get_content` | artifact_id, version? | Get version content |
| `artifact.list` | session_id, include_deleted?, project_id?, metadata_filter? | List session artifacts |
| `artifact.delete` | artifact_id, soft_delete?, project_id? | Delete artifact |
| `artifact.update` | artifact_id, content, change_log?, source? | Create new version |
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

**Get Content** — serve artifact content for iframe:
```bash
curl http://127.0.0.1:8892/api/artifact/content/art_xxx
```
Returns raw content with appropriate `Content-Type` header.

**Interact** — bidirectional canvas → chat context:
```bash
curl -X POST http://127.0.0.1:8892/api/artifact/interact \
  -H "Content-Type: application/json" \
  -d '{"artifact_id":"art_xxx","action":"user_edit","payload":{"content":"<!DOCTYPE html><html><body>Updated</body></html>"}}'
```
Actions: `user_click`, `user_edit`, `state_change`. Creates a new artifact version and returns a `ref_text` for chat context injection.

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

## Token Counting Strategy

Three-tier fallback:
1. fusion-mlx `/v1/messages/count_tokens` — most accurate
2. tiktoken (cl100k_base) — fast, good approximation
3. `len(text) // 4` — heuristic fallback

## Architecture

```
Product Layer: Fusion Code (tool_use) │ Fusion Studio (GUI)
─────────────────────────────────────────────────────────────
Middleware: Artifacts Engine (Python daemon, JSON-RPC 2.0)
─────────────────────────────────────────────────────────────
Storage: SQLite + Filesystem
─────────────────────────────────────────────────────────────
Inference: fusion-mlx (local) │ Cloud APIs
```

## Roadmap

The refactoring blueprint to make fusion-artifacts-engine competitive with Claude Artifacts is documented in [fusion-artifact-enhance-ar.md](./fusion-artifact-enhance-ar.md) (AR = Architecture Refactor). It covers:

- Competitive gap matrix vs Claude Artifacts (追平 9 项 / 超越 5 项 / 二期 1 项)
- Data model expansion, new engine ops (rename/star/pin/duplicate/snapshot/share/recycle/migrate-KB), rendering hardening (CSP sandbox, Mermaid/Markdown localization), ```artifact fence parsing, SSE event bus, REST `/api/v1` parity, optimistic locking
- 4-phase implementation plan (P1-P4)
- Cross-repo issue/PR proposals for fusion-studio / fusion-agent-studio / fusion-projects / fusion-mlx / fusion-cowork (GUI designs in md ASCII mockups)

## Running Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

## License

Apache License 2.0

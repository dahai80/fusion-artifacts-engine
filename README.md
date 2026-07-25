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
| `artifact.create` | session_id, name, type, content, summary? | Create artifact + v1 |
| `artifact.get` | artifact_id | Get metadata |
| `artifact.get_content` | artifact_id, version? | Get version content |
| `artifact.list` | session_id, include_deleted? | List session artifacts |
| `artifact.delete` | artifact_id, soft_delete? | Delete artifact |
| `artifact.update` | artifact_id, content, change_log? | Create new version |
| `artifact.version_list` | artifact_id | List all versions |
| `artifact.version_rollback` | artifact_id, target_version | Rollback to version |
| `artifact.inject` | messages, max_context? | Pre-request content injection |
| `artifact.check_safety` | messages, max_context? | Token safety check |
| `artifact.export` | artifact_id, include_versions? | Export artifact data |
| `artifact.export_session` | session_id, output_dir | Batch export session |
| `artifact.import` | session_id, data | Import artifact |
| `ping` | — | Health check |

## Python SDK

```python
import asyncio
from fusion_artifacts_engine import ArtifactEngine, ArtifactEngineConfig

engine = ArtifactEngine()

async def main():
    # Create
    art, ver, ref = await engine.create_artifact(
        "sess_1", "main.py", "code", "print('hello')\n" * 50,
        summary="Main application"
    )
    print(f"Created: {art.id} v{ver.version_num}")
    print(f"Reference: {ref}")

    # Update
    v2, ref2 = await engine.create_version(art.id, "print('world')\n" * 60, "Updated")

    # Inject for model context
    messages = [{"role": "assistant", "content": ref}]
    injected, total, safe = await engine.inject(messages)

    # Safety check
    safe, current, remaining = await engine.check_safety(messages)

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

## Running Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

## License

Proprietary — Fusion internal component

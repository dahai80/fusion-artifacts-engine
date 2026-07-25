# Fusion Artifacts Engine — Revised Design

> Benchmarking Claude Artifacts, adapted to Fusion's real architecture.

## 1. Problem: Original Design vs Reality

| Item | ar.md/interface.md | Reality (upstream code) | Resolution |
|---|---|---|---|
| Token counting | `litellm.token_counter` | fusion-code does NOT use litellm | Use tiktoken + fusion-mlx `/v1/messages/count_tokens` |
| Reference protocol | XML `<artifact>` tags in messages | Models generate XML unreliably; Claude uses tool_use records | Register `create_artifact` as tool_use; references are tool_result records |
| Frontend trigger | Frontend regex on stream chunks | Fragile; misses edge cases | Backend tool_use is authoritative; frontend gets notified via JSON-RPC events |
| Artifact types | code/markdown/html/data | Claude supports React type | Add `react` type |
| IPC protocol | Not specified | fusion-studio uses JSON-RPC 2.0 over Unix Socket | Expose JSON-RPC 2.0 daemon |
| Daemon pattern | Not specified | mlx-daemon = Python service + JSON-RPC | Follow same pattern |
| Content offload | Custom mechanism | fusion-code already has `toolResultStorage.ts` | Complement, don't replace; artifacts engine handles structured versioned assets |

## 2. Architecture

```
┌────────────────────────────────────────────────────────────┐
│  Product Layer                                             │
│  Fusion Code (tool_use client) │ Fusion Studio (GUI)       │
├────────────────────────────────────────────────────────────┤
│  Artifacts Engine (Python daemon, JSON-RPC 2.0)           │
│  ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌─────────────┐ │
│  │ Artifact │ │ Version   │ │ Injection│ │ Auto-       │ │
│  │ CRUD     │ │ Manager   │ │ Safety   │ │ Identifier  │ │
│  └──────────┘ └───────────┘ └──────────┘ └─────────────┘ │
│  ┌──────────┐ ┌───────────┐ ┌──────────┐                 │
│  │ Token    │ │ Ref       │ │ Import/  │                 │
│  │ Counter  │ │ Parser    │ │ Export   │                 │
│  └──────────┘ └───────────┘ └──────────┘                 │
├────────────────────────────────────────────────────────────┤
│  Storage (SQLite + filesystem)                             │
│  ~/.fusion/artifacts/meta.db + content/{art_id}/v*.ext     │
└────────────────────────────────────────────────────────────┘
         │                           │
         ▼                           ▼
  fusion-mlx (local)         Cloud APIs (GLM/Claude)
  /v1/messages/count_tokens  (token counting fallback)
```

### 2.1 Integration Points

```
fusion-code                    artifacts-engine              fusion-studio
    │                               │                            │
    │  tool_use: create_artifact    │                            │
    │──────────────────────────────▶│                            │
    │  tool_result: ref metadata    │                            │
    │◀──────────────────────────────│                            │
    │                               │  JSON-RPC: artifact.created │
    │                               │───────────────────────────▶│
    │                               │                            │
    │  tool_use: update_artifact    │                            │
    │──────────────────────────────▶│                            │
    │  tool_result: new version ref │                            │
    │◀──────────────────────────────│                            │
    │                               │  JSON-RPC: artifact.updated │
    │                               │───────────────────────────▶│
    │                               │                            │
    │  pre-request: inject_content  │                            │
    │──────────────────────────────▶│                            │
    │  enriched messages + safety   │                            │
    │◀──────────────────────────────│                            │
    │                               │                            │
    │                               │  JSON-RPC: artifact.get     │
    │                               │◀───────────────────────────│
    │                               │  artifact data              │
    │                               │───────────────────────────▶│
```

## 3. Reference Protocol: tool_use, Not XML

### 3.1 Why tool_use

- Claude Artifacts uses tool_use internally — model calls a tool, tool returns structured result
- Models are trained to generate tool_use calls reliably; XML tag generation is unreliable
- fusion-code already has mature Anthropic-style tool_use pipeline with 30+ tools
- tool_use results are native Anthropic Messages API content blocks — no parsing fragility

### 3.2 tool_use Schema for create_artifact

```json
{
    "name": "create_artifact",
    "description": "Create an artifact for code, documents, HTML apps, or data files. Use when generating content >30 lines of code or >1500 chars of text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Filename or document title"
            },
            "type": {
                "type": "string",
                "enum": ["code", "markdown", "html", "react", "data"],
                "description": "Artifact type"
            },
            "content": {
                "type": "string",
                "description": "Full content of the artifact"
            },
            "summary": {
                "type": "string",
                "description": "Brief description (max 200 chars)"
            }
        },
        "required": ["name", "type", "content"]
    }
}
```

### 3.3 tool_use Schema for update_artifact

```json
{
    "name": "update_artifact",
    "description": "Update an existing artifact with new content, creating a new version.",
    "input_schema": {
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": "The artifact ID (art_xxx)"
            },
            "content": {
                "type": "string",
                "description": "Updated full content"
            },
            "change_log": {
                "type": "string",
                "description": "What changed in this version"
            }
        },
        "required": ["artifact_id", "content"]
    }
}
```

### 3.4 tool_result Format (returned to model)

```json
{
    "type": "tool_result",
    "tool_use_id": "...",
    "content": "[Artifact: fusion_agent.py | ID: art_abc123 | Version: v1 | Type: code | Tokens: 4200 | Summary: Agent main logic with tool calls and memory module]"
}
```

This is ~30 tokens. The full content is stored in the engine, never in chat history.

## 4. Token Counting: No litellm Dependency

### 4.1 Strategy

Priority order for token counting:

1. **fusion-mlx local endpoint**: `POST /v1/messages/count_tokens` — most accurate for local models
2. **tiktoken**: Cl100K base tokenizer — fast, no network, good approximation
3. **Character heuristic**: `len(text) // 4` — fallback only

### 4.2 Implementation

```python
class TokenCounter:
    def __init__(self, mlx_url: str = "http://localhost:8890"):
        self.mlx_url = mlx_url
        self._tiktoken = None

    async def count(self, text: str, model: str = None) -> int:
        try:
            return await self._count_via_mlx(text)
        except Exception:
            return self._count_via_tiktoken(text)

    async def _count_via_mlx(self, text: str) -> int:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.mlx_url}/v1/messages/count_tokens",
                json={"model": "default", "messages": [{"role": "user", "content": text}]},
                timeout=5.0
            )
            return resp.json().get("total_tokens", 0)

    def _count_via_tiktoken(self, text: str) -> int:
        if self._tiktoken is None:
            import tiktoken
            self._tiktoken = tiktoken.get_encoding("cl100k_base")
        self._tiktoken = self._tiktoken
        return len(self._tiktoken.encode(text))
```

### 4.3 Context Safety Check

```python
async def check_context_safety(
    self,
    messages: list[dict],
    reserve_output_tokens: int = 8192,
    max_context: int = 180000
) -> tuple[bool, int, int]:
    total_input = sum(await self.count(m.get("content", "")) for m in messages)
    remaining = max_context - total_input - reserve_output_tokens
    return (remaining > 0, total_input, remaining)
```

## 5. JSON-RPC 2.0 API

Following the fusion-studio IPC pattern. Daemon listens on `/tmp/fusion-artifacts.sock`.

### 5.1 RPC Methods

| Method | Params | Returns | Description |
|---|---|---|---|
| `artifact.create` | `{session_id, name, type, content, summary?}` | `Artifact` | Create artifact + v1 |
| `artifact.get` | `{artifact_id}` | `Artifact` | Get metadata |
| `artifact.get_content` | `{artifact_id, version?}` | `{content, token_count}` | Get version content |
| `artifact.list` | `{session_id, include_deleted?}` | `[Artifact]` | List session artifacts |
| `artifact.delete` | `{artifact_id, soft_delete?}` | `{ok: bool}` | Delete artifact |
| `artifact.update` | `{artifact_id, content, change_log?}` | `ArtifactVersion` | Create new version |
| `artifact.version_list` | `{artifact_id}` | `[ArtifactVersion]` | List versions |
| `artifact.version_rollback` | `{artifact_id, target_version}` | `ArtifactVersion` | Rollback to version |
| `artifact.inject` | `{messages, artifact_ids?, max_context?}` | `{messages, total_tokens, safe}` | Pre-request injection |
| `artifact.check_safety` | `{messages, max_context?}` | `{safe, current_tokens, remaining}` | Token safety check |
| `artifact.export` | `{artifact_id, include_versions?}` | `{path}` | Export to file |
| `artifact.export_session` | `{session_id, output_dir}` | `{count, path}` | Batch export |
| `artifact.import` | `{session_id, data}` | `Artifact` | Import artifact |

### 5.2 Server-Sent Notifications (push to fusion-studio)

| Method | Params | When |
|---|---|---|
| `artifact.created` | `{artifact_id, name, type, session_id}` | New artifact created |
| `artifact.updated` | `{artifact_id, version, name}` | New version created |
| `artifact.deleted` | `{artifact_id}` | Artifact deleted |

### 5.3 JSON-RPC Message Format

```json
// Request
{"jsonrpc": "2.0", "method": "artifact.create", "params": {...}, "id": 1}

// Response
{"jsonrpc": "2.0", "result": {...}, "id": 1}

// Notification (no id)
{"jsonrpc": "2.0", "method": "artifact.created", "params": {...}}
```

## 6. Data Model

### 6.1 artifacts table

```sql
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('code','markdown','html','react','data')),
    current_version INTEGER NOT NULL DEFAULT 1,
    summary TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    is_deleted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(type);
```

### 6.2 artifact_versions table

```sql
CREATE TABLE IF NOT EXISTS artifact_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    version_num INTEGER NOT NULL,
    content TEXT DEFAULT '',
    content_path TEXT,
    token_count INTEGER NOT NULL DEFAULT 0,
    change_log TEXT DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_versions_artifact ON artifact_versions(artifact_id);
```

### 6.3 Storage Paths

```
~/.fusion/artifacts/
├── meta.db                          # SQLite
├── content/
│   ├── art_abc123/
│   │   ├── v1.py                    # content_path for large files
│   │   ├── v2.py
│   │   └── ...
│   └── art_def456/
│       └── v1.html
```

Small content (<10KB) stored inline in `content` column. Large content stored to `content_path`, column is empty.

## 7. Project Structure

```
fusion_artifacts_engine/
├── __init__.py
├── __main__.py                  # CLI entry: python -m fusion_artifacts_engine
├── config.py                    # ArtifactEngineConfig
├── engine.py                    # ArtifactEngine core
├── models.py                    # Artifact, ArtifactVersion, ArtifactRef
├── token_counter.py             # TokenCounter (mlx + tiktoken + fallback)
├── ref_parser.py                # Parse tool_result refs, generate ref metadata
├── injection.py                 # inject_artifacts_to_messages + safety check
├── auto_identifier.py           # should_create_artifact logic
├── storage/
│   ├── __init__.py
│   ├── base.py                  # StorageDriver ABC
│   └── sqlite_storage.py        # SQLite + filesystem implementation
├── rpc/
│   ├── __init__.py
│   ├── server.py                # JSON-RPC 2.0 server (Unix socket)
│   └── methods.py               # RPC method handlers
├── tool_schemas/
│   ├── __init__.py
│   ├── create_artifact.py       # tool_use schema definition
│   └── update_artifact.py       # tool_use schema definition
└── utils.py                     # ID generation, logging setup

tests/
├── test_engine.py
├── test_token_counter.py
├── test_storage.py
├── test_injection.py
├── test_rpc.py
└── test_auto_identifier.py
```

## 8. Upstream Dependencies (PRs/Issues needed)

### 8.1 fusion-code (PR required)

1. **Register `create_artifact` tool** — Add to tool list alongside existing 30+ tools
2. **Register `update_artifact` tool** — Add to tool list
3. **Hook pre-request injection** — In the message assembly pipeline (where `fusion-mlx-adapter.ts` builds the request), call `artifact.inject` RPC to resolve references
4. **Handle tool_result** — When `create_artifact`/`update_artifact` tool_use completes, store the tool_result reference in conversation (don't store full content)

### 8.2 fusion-studio (PR required)

1. **Add artifact RPC methods to IPCClient.swift** — `artifact.*` methods in the JSON-RPC client
2. **Implement ArtifactsPanel.swift** — Replace placeholder with real UI using artifact RPC
3. **Subscribe to artifact notifications** — Listen for `artifact.created`/`artifact.updated`/`artifact.deleted` push notifications

### 8.3 fusion-mlx (Issue required)

1. **Verify `/v1/messages/count_tokens` endpoint** — Confirm it exists and returns accurate counts. If missing, file issue to add it.

## 9. MVP Implementation Plan (2 weeks)

### Week 1: Core Engine

| Day | Task | Deliverable |
|---|---|---|
| 1 | Project scaffold + config + models | `config.py`, `models.py`, `utils.py` |
| 2 | Storage layer (SQLite + filesystem) | `storage/base.py`, `storage/sqlite_storage.py` |
| 3 | Engine CRUD (create, get, list, delete) | `engine.py` core methods |
| 4 | Version management + TokenCounter | `token_counter.py`, version methods in engine |
| 5 | Injection + safety + ref parser | `injection.py`, `ref_parser.py`, `auto_identifier.py` |

### Week 2: RPC + Integration

| Day | Task | Deliverable |
|---|---|---|
| 6 | JSON-RPC server + all method handlers | `rpc/server.py`, `rpc/methods.py` |
| 7 | Tool schemas + CLI | `tool_schemas/`, `__main__.py` |
| 8 | Tests: engine + storage + injection | `tests/` |
| 9 | Tests: RPC + integration | `tests/test_rpc.py` |
| 10 | End-to-end verification + docs | 10-round iteration test, README.md |

### MVP Verification Criteria

- 10 rounds of 1000-line code iteration
- Chat token usage stays ~15k (vs ~120k without artifacts)
- All RPC methods functional via Unix socket
- tool_use schemas validated against fusion-code tool pipeline
- Token counting works with fusion-mlx endpoint and tiktoken fallback

## 10. Key Constants

| Constant | Value | Purpose |
|---|---|---|
| Safe context threshold | 180,000 | Max input tokens before injection |
| Output reserve | 8,192 | Tokens reserved for model output |
| Auto-create lines (code) | 30 | Lines threshold for code artifacts |
| Auto-create chars (text) | 1,500 | Character threshold for text artifacts |
| Small content limit | 10,240 | Bytes; below = inline in SQLite |
| Artifact ID prefix | `art_` | ID format: art_{random} |
| RPC socket path | `/tmp/fusion-artifacts.sock` | Unix domain socket |
| Storage root | `~/.fusion/artifacts/` | Local storage directory |
| DB path | `~/.fusion/artifacts/meta.db` | SQLite database |

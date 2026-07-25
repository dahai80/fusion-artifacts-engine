# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**fusion-artifacts-engine** is a generic middleware component in the Fusion "one-core, nine-endpoints" architecture. It sits between the model inference layer (fusion-mlx / cloud APIs) and the product layer (Fusion Code, Fusion Design, etc.). Its job is to physically separate generated artifacts (code, documents, HTML apps, data files) from chat messages to solve context overflow.

## Commands

```bash
source .venv/bin/activate   # Enter project environment (required first)
pip install -e .             # Install editable
pip install -e ".[test]"     # Install with test dependencies
pytest tests/ -v             # Run all tests
pytest tests/test_engine.py -v  # Run single test file
fusion-artifacts-engine start --port 8892   # Start daemon
fusion-artifacts-engine status             # Check if running
fusion-artifacts-engine version            # Print version
```

## Architecture

Python package under `fusion_artifacts_engine/`:

- **`config.py`** — `ArtifactEngineConfig` (storage paths, thresholds, MLX URL)
- **`models.py`** — `Artifact`, `ArtifactVersion`, `ArtifactRef` (Pydantic models), `ArtifactType` literal
- **`engine.py`** — `ArtifactEngine` core: create_artifact, create_version, get_version_content, rollback_version, inject, check_safety, should_create_artifact
- **`token_counter.py`** — `TokenCounter`: fusion-mlx endpoint → tiktoken → heuristic fallback
- **`ref_parser.py`** — Parse `[Artifact: ...]` tool_result refs and XML `<artifact>` refs; `generate_ref_text()`
- **`injection.py`** — `inject_artifacts_to_messages()`: replace ref tags with full content for model context
- **`auto_identifier.py`** — `should_create_artifact()`, `detect_artifact_type()`, `extract_name_hint()`
- **`storage/`**
  - `base.py` — `StorageDriver` ABC
  - `sqlite_storage.py` — `SQLiteStorage`: SQLite + filesystem, content <10KB inline, >10KB to disk
- **`rpc/`**
  - `server.py` — `ArtifactRPCServer`: HTTP-based JSON-RPC 2.0 server
  - `methods.py` — `RPCHandler`: 14 RPC method dispatchers
- **`tool_schemas/`**
  - `create_artifact.py` — tool_use JSON schema for create_artifact
  - `update_artifact.py` — tool_use JSON schema for update_artifact
- **`utils.py`** — `generate_artifact_id()`, `setup_logging()`
- **`__main__.py`** — CLI entry point (start/status/version)

## Reference Protocol

References use tool_result format (not XML tags):

```
[Artifact: test.py | ID: art_abc123 | Version: v1 | Type: code | Tokens: 4200 | Summary: Agent main logic]
```

~30 tokens per ref vs ~4200 tokens for full content (~99% savings).

## JSON-RPC API

HTTP server on `127.0.0.1:8892`. Methods:

| Method | Description |
|---|---|
| `artifact.create` | Create artifact + v1 |
| `artifact.get` | Get metadata |
| `artifact.get_content` | Get version content |
| `artifact.list` | List session artifacts |
| `artifact.delete` | Soft/hard delete |
| `artifact.update` | Create new version |
| `artifact.version_list` | List versions |
| `artifact.version_rollback` | Rollback to version |
| `artifact.inject` | Pre-request content injection |
| `artifact.check_safety` | Token safety check |
| `artifact.export` | Export artifact data |
| `artifact.export_session` | Batch export session |
| `artifact.import` | Import artifact |
| `ping` | Health check |

## Key Constants

| Constant | Value |
|---|---|
| Safe context threshold | 180,000 tokens |
| Output reserve | 8,192 tokens |
| Auto-create lines (code) | 30 |
| Auto-create chars (text) | 1,500 |
| Small content limit | 10,240 bytes |
| Artifact ID prefix | `art_` |
| Default RPC port | 8892 |
| Storage root | `~/.fusion/artifacts/` |
| DB path | `~/.fusion/artifacts/meta.db` |
| Supported types | code, markdown, html, react, data |

## Data Model

**artifacts table**: id (PK), session_id, name, type, current_version, summary, created_at, updated_at, is_deleted

**artifact_versions table**: id (PK auto), artifact_id (FK), version_num, content, content_path, token_count, change_log, created_at

## Upstream Dependencies

- **fusion-code** (PR): Register create_artifact/update_artifact tools, hook pre-request injection
- **fusion-studio** (PR): Add artifact.* RPC to IPCClient, implement ArtifactsPanel
- **fusion-mlx** (Issue): Verify /v1/messages/count_tokens endpoint availability

## Conventions

- Python >= 3.11, 4-space indentation (multiples of 4)
- All classes use `logging.getLogger(__name__)` — no print statements
- No docstrings
- Pydantic models for data validation
- Tests in `tests/`, pytest with pytest-asyncio
- Files must not exceed 3000 lines

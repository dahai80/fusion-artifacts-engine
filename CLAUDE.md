---
doc_id: fusion-artifacts-engine/guide
product: fusion-artifacts-engine
class: app
entry: service
doc_type: guide
title: { zh: "fusion-artifacts-engine 开发者指南", en: "fusion-artifacts-engine developer guide" }
audience: engineer
status: draft
version: 0.7.13
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - CLAUDE.md
---

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

`fusion-artifacts-engine` is a structured artifact CRUD middleware for the Fusion architecture. It physically separates generated artifacts (code, docs, HTML apps) from chat messages, replacing long content in conversations with a lightweight reference (~30 tokens) and returning content on-demand. This solves context overflow in long AI conversations: ~15k tokens instead of ~120k over 10 rounds of 1000-line code iteration.

It is one of 27 sub-projects in the Fusion monorepo. Callers: **fusion-studio** (GUI, auto-starts it via `start.sh` on launch) and **fusion-code** (tool_use). See the parent `/Users/dahai/fusion/CLAUDE.md` for the shared monorepo environment (`source .venv/bin/activate` at repo root is required before any Python work).

## Common Commands

```bash
# All commands assume the repo-root venv is activated:
source /Users/dahai/fusion/.venv/bin/activate

# Build (editable install with test extras)
pip install -e ".[all]"   # test + otel + ruff; for production without test/ruff use ".[otel]"

# Run full suite (asyncio_mode=auto, testpaths=["tests"])
pytest tests/ -v

# Single test
pytest tests/test_engine.py::test_create_artifact -v

# Coverage
pytest tests/ --cov=fusion_artifacts_engine --cov-report=term-missing

# Lint (ruff configured in pyproject.toml [tool.ruff]; line-length 100, py311 target)
ruff check .
ruff format .

# Daemon lifecycle (preferred for running the service)
./start.sh start      # launches on 127.0.0.1:11451, waits for JSON-RPC ping health
./start.sh status     # exits 0 if healthy, 1 if not
./start.sh stop | restart

# Or run the CLI directly (start.sh wraps this)
fusion-artifacts-engine start --host 127.0.0.1 --port 11451
fusion-artifacts-engine status
fusion-artifacts-engine version
```

The daemon listens on `127.0.0.1:11451`. It exposes several HTTP interfaces on the **same port**: JSON-RPC 2.0 (POST to `/`), REST `/api/v1/*` (artifacts CRUD, versions, list-by-session), SSE streams — global `/api/v1/events/stream`, artifact-scoped `/api/v1/artifacts/{id}/events`, session-scoped `/api/v1/sessions/{id}/events`, each accepting a `?kind=` filter (#55); `/healthz` (unauthenticated liveness, always 200 — used by Docker `HEALTHCHECK`), `/readyz` (readiness — checks storage + event_bus), and `/metrics` (Prometheus text exposition, optional `X-Metrics-Token` auth). Public share endpoint `GET /api/v1/share/{share_id}` is unauthenticated (rate-limited via the `public` bucket). Built on stdlib `http.server` (ThreadingMixIn) — no FastAPI/uvicorn. Ruff is configured in `pyproject.toml` (`[tool.ruff]`, line-length 100, py311 target; `select` is conservative — correctness/consistency rules only, CJK-aware ignores).

### Test conventions

Tests construct an `ArtifactEngine` against a temp `storage_root` (no real daemon needed). The pattern is a `tmp_dir` fixture (`tempfile.mkdtemp` + `shutil.rmtree` teardown) and an `engine` fixture wrapping `ArtifactEngineConfig(storage_root=tmp_dir / "artifacts")`. Tests are async (`asyncio_mode=auto`); async engine methods are `await`ed directly. **Clean up process data after verification** — only final artifacts and logs should remain.

## Architecture

```
CLI (__main__.py) ──load_config()──▶ ArtifactEngine(config)
                                            │
                              ArtifactRPCServer(engine)  ── stdlib HTTP server
                                            │
                          JSONRPCHandler ──▶ RPCHandler (rpc/methods.py)
                                            │
                                method_map: "artifact.*" / "context.budget" / "render.visual" / "ping"
                                            │
                              StorageDriver (storage/base.py, abstract)
                                            │
                              SQLiteStorage (WAL, metadata + inline content)
                                            │
                              Filesystem (content/{art_id}/v{num}.{ext})
                                            │
                              EventBus (rpc/event_bus.py) ──▶ SSE /api/v1/events/stream
```

### Layers

- **`__main__.py`** — CLI entry (`fusion-artifacts-engine`). Parses `start`/`status`/`version`, loads config, wires `ArtifactEngine` → `ArtifactRPCServer`, installs SIGINT/SIGTERM shutdown. `status` does a `ping` JSON-RPC over `httpx`.
- **`config.py`** — `ArtifactEngineConfig` (pydantic, `extra="forbid"`). `load_config()` merges defaults from `default_config.yaml` → user config at `~/.fusion/artifacts/config.yaml` (or `FUSION_ARTIFACTS_CONFIG`) → env overrides (`FUSION_ARTIFACTS_HOST`/`_PORT`/`_STORAGE_ROOT`). Stale user-config keys are stripped.
- **`engine.py`** (~1350 lines) — the core `ArtifactEngine`. All artifact domain logic: create/version/update/patch/load, auto-compact, version_diff, render/check_safety/inject/interact/sync, lifecycle (rename/star/pin/duplicate), recycle, share, folders, tags, events, external-source, watch, snapshots, export/import. Most async methods delegate to `SQLiteStorage`; share logic delegates to `ShareManager` (`share.py`). **This is the file to edit for domain behavior.**
- **`rpc/server.py`** — `JSONRPCHandler` (stdlib `BaseHTTPRequestHandler`). Auth via `X-API-Key` (constant-time `hmac.compare_digest`) against `FUSION_ARTIFACTS_API_KEY` env var; fail-closed unless `allow_no_auth=True`. Routes JSON-RPC (POST `/`), REST `/api/v1/*` (GET/POST), and SSE. Sets a strict CSP header on rendered share responses.
- **`rpc/methods.py`** — `RPCHandler` with an explicit `_method_map` dict mapping every JSON-RPC method name to a handler. **Adding a new RPC method = add an entry here + a handler method + an engine method.** Handlers validate params, call the engine, and `event_bus.publish(...)` for state-changing ops. See the README method table for the full surface.
- **`rpc/event_bus.py`** — thread-safe `EventBus` (a `queue.Queue` per subscriber, maxsize 256). SSE handler calls `subscribe()` (no args) and drains the queue; **handlers must `publish()` with the event `kind`/data so SSE delivery works** (see memory `fae-sse-event-bus-contract`). Full subscribers are dropped.
- **`storage/sqlite_storage.py`** (~2630 lines) — the storage implementation. SQLite in **WAL** mode. **Read pool** of 4 connections (WAL allows concurrent readers) + a **single write connection** serialized via `BEGIN IMMEDIATE` (engine drives thread-safety, `check_same_thread=False`). Background PASSIVE WAL checkpoint (default 300s; TRUNCATE on close). SQLite errors are translated to `RpcError` codes: "locked" → `ConflictError` (-32002, retry), "disk/I/O/full" → `ResourceLimitError` (-32003, retry/degrade). Metadata in `meta.db`; content below `small_content_limit` (default 10KB) stored **inline** in SQLite, larger content written to `content/{art_id}/v{num}.{ext}`. Metadata filtered via `json_extract(metadata, ?)`, with optional expression indexes on `metadata_indexed_keys`. `storage/base.py` defines the `StorageDriver` ABC.
- **`models.py`** — pydantic models: `Artifact`, `ArtifactVersion`, `ArtifactShare`, `ArtifactFolder`, `ArtifactTag`, `ArtifactEvent`, plus `infer_kind()` (type→kind mapping).
- **`auto_identifier.py`** — `should_create_artifact()`: threshold + renderable-type (SVG/Mermaid/React/HTML) detection that decides whether content becomes an artifact.
- **`compactor.py`** / **`token_counter.py`** (`tiktoken` cl100k_base) / **`ref_parser.py`** (reference text generation) / **`section_index.py`** — supporting modules.
- **`render.py`** — renders public share HTML preview (markdown via `markdown`, HTML sanitized via `lxml` + `lxml_html_clean` — real parser, not regex). Source isolation: rendered output never echoes raw artifact bytes inline.
- **`share.py`** — `ShareManager`: share create/revoke/get + access control (expiry, max_accesses, owner-only share, `permission` view/remix — re-share upserts permission on the same link). Public-share access counting is **memory-buffered** (background flush thread) to avoid write-lock contention under viral load; atomic check-and-increment closes the max_accesses TOCTOU. Extracted from `engine.py` (engine holds a `ShareManager` and delegates).
- **`gateway_client.py`** (#75) — web-shell integration: self-registers the shell manifest to fusion-gateway `POST /api/v1/shell/manifests` (self-keyed with the engine `api_key`, `admin_key` bootstrap fallback) + 15s heartbeat thread with 404-triggered re-registration. Started by `__main__.py` when `gateway.enabled`; all failures non-fatal.
- **`webui/` + `webui_dist/`** (#76) — built-in web GUI (Vite + React 18 + Tailwind v4, source in `webui/`, compiled dist committed as package-data in `fusion_artifacts_engine/webui_dist/`). Served same-origin by `rpc/server.py` static path (`no-store` entry + strict CSP with gateway `frame-ancestors`, `immutable` assets, traversal guard). Rebuild: `cd webui && bun run build`. GUI consumes existing JSON-RPC only (library + detail views, share permission select, remix + lineage).
- **`tracing.py`** — optional OTel distributed tracing (P2-5). Pure no-op when `opentelemetry-sdk` not installed or `tracing.enabled=false`; real spans (`rpc.server.duration`, `db.storage.duration`) when enabled. Callers use `with tracing.span("name", **attrs):` uniformly.
- **`metrics.py`** — self-implemented Prometheus text exposition (`MetricsRegistry`: counters/gauges/histograms, no external dep). Served at `/metrics`.
- **`rate_limiter.py`** — token-bucket rate limiter, two buckets: `default` (JSON-RPC + authed REST) and `public` (unauthed share endpoints, isolated quota). `rps=0` = unlimited.
- **`token_budget.py`** — context-budget accounting for `context.budget` RPC method.
- **`utils.py`** — shared helpers (content hashing, ext inference, etc).
- **`rpc/errors.py`** — `RpcError` hierarchy with **layered error codes**: JSON-RPC standard (-32700~-32603) + business codes `-32001` not-found / `-32002` conflict (retry) / `-32003` resource-limit (retry) / `-32004` business-rule / `-32005` not-implemented / `-32006` permission/IDOR. Callers branch on `code` for retry-vs-abandon.
- **`Dockerfile`** — containerized deployment (`python:3.12-slim`, installs `.[otel]`, `/data` volume for SQLite+content, binds `0.0.0.0:11451`, `HEALTHCHECK` hits `/healthz`). Referenced by `docker-compose.business.yml`.
- **`dsl_parser.py`** (#60) — DSL v3.0 parser. `parse_model_output(raw_text)` extracts `<thinking>`/`<dsl>` tags from raw model output, parses JSON, validates via `fusion_core.dsl_schema.validate_dsl_str()`. Returns `ParsedDSL(thinking, dsl, valid, errors, visual_type, fallback_type)`. Also `extract_visual_type()`, `get_pipeline_progress()`. No `eval()`/`exec()` on model output (security).
- **`visual_primitives/`** (#58) — 8 HTML compiler modules, each extends `BaseCompiler` (`base.py`). `compile(dsl_dict, progress) -> str` returns self-contained HTML+SVG+CSS (Three.js/Chart.js via CDN for 3D/charts). Registry in `__init__.py`: `COMPILER_REGISTRY` maps `visual_type` → compiler class. `FlowCardCompiler` is the universal fallback (never throws, renders any pipeline). `TapeDiagramCompiler` is the core fallback (handles generic quantity relationships).
- **`visual_dispatcher.py`** (#59) — `VisualEngineDispatcher`: routes `dsl.meta.visual_type` to the correct compiler with a **3-level fallback chain** (primary → TapeDiagram → FlowCard, 100% render guarantee). `dsl.meta.fallback_type` overrides level 2. `render()` method is the full entry point (handles raw_text or dsl dict, returns result dict with html/visual_type/valid/errors).
- **`visual_error_boundary.py`** (#59) — `VisualErrorBoundary`: wraps compiler calls, catches exceptions, logs DSL metadata (visual_type, meta.id), returns fallback compiler output. Never propagates exceptions.
- **`render.visual`** RPC method + `POST /api/v1/render-visual` REST endpoint + `GET /api/v1/render-visual/stream` SSE — visual rendering API surface. SSE streams HTML fragments per pipeline step (progress-driven).

### Key data path

Storage root defaults to `~/.fusion/artifacts`:
- `meta.db` — SQLite metadata + inline small content
- `content/{art_id}/v{num}.{ext}` — large version content on disk

Artifact IDs are `art_*` (configurable `artifact_id_prefix`); share IDs `shr_*`.

### Configuration

All tunables live in `default_config.yaml` (shipped, package-data) overridable by `~/.fusion/artifacts/config.yaml` or env vars. Do **not** hardcode config values in code — read them through `ArtifactEngineConfig`. Sections: `server` (`max_workers` thread-pool cap), `storage` (`small_content_limit`, `sync_root`, `disk_space_warning_pct`, `wal_checkpoint_interval`, `metadata_indexed_keys` for json_extract indexes), `thresholds`, `artifact` (`max_versions_per_artifact`, `max_content_bytes`, `max_metadata_bytes`, `max_event_payload_bytes`), `security` (`api_key`, `allow_no_auth`, `recycle_retention_days`, `share_max_ttl_days`), `sse` (`heartbeat_interval`, `max_lifetime`, `max_event_bytes`, `max_connections` — SSE has its own concurrency semaphore separate from the RPC worker pool), `rate_limit` (`rps`/`burst` default + `public_rps`/`public_burst`), `metrics` (`enabled`, `token`), `tracing` (`enabled`, `service_name`).

## Conventions

- **No docstrings.** 4-space indentation (multiples of 4). Always log (`logging.getLogger(__name__)`) for problem localization.
- Match the existing method-registration pattern in `rpc/methods.py` when extending the API.
- `asyncio_mode=auto` — async test functions need no decorator.
- Version source tracking: each version records `source` (`manual` | `ai_generation`); `change_log` is auto-generated from the diff when omitted.
- Optimistic locking: `artifact.update` accepts `expected_content_hash`; mismatch → error (prevents lost updates).
- Tests are required but not the goal; a test that passes for the wrong reason is worse than none.

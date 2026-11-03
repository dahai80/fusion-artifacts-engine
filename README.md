# Fusion Artifacts Engine

English | **[Chinese](./README_CN.md)**

Structured artifact CRUD middleware for the Fusion architecture. Physically separates generated artifacts from chat messages to solve context overflow in long AI conversations.

> **New: [Usage Guide](./docs/USAGE_GUIDE.md)** — bilingual (EN + CN), scenario-based walkthrough with runnable examples covering create/version/share/render/budget/lifecycle/recycle/ops.

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

## Container Deployment

A `Dockerfile` is shipped for containerized deployment (used by the
multi-node compose stack). The image runs the same stdlib HTTP server on
port 11451 — no FastAPI/uvicorn — and persists all data to a `/data` volume.

```bash
# Build
docker build -t fusion-artifacts-engine .

# Run (persist artifacts to a named volume, bind 127.0.0.1 only)
docker run -d --name fae \
    -p 127.0.0.1:11451:11451 \
    -v fae-data:/data \
    -e FUSION_ARTIFACTS_API_KEY="$API_KEY" \
    fusion-artifacts-engine

# Healthcheck (liveness, no auth)
curl -sf http://127.0.0.1:11451/healthz && echo OK

# Authenticated JSON-RPC ping
curl -s http://127.0.0.1:11451 \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    -d '{"jsonrpc":"2.0","method":"ping","id":1}'
```

Notes:

- Image base: `python:3.12-slim`; installs `.[otel]` (no test/ruff extras).
  Approx. 262 MB built.
- The engine does **not** call MLX inference, so no `FUSION_MLX_URL` is set.
  Callers needing MLX carry their own client env.
- Auth is fail-closed by default. Set `FUSION_ARTIFACTS_API_KEY` (env, shown
  above) or mount a config file with `security.api_key`. The
  `allow_no_auth` flag has no env override on purpose — set it in the config
  file only if you understand the risk.
- `HEALTHCHECK` probes `/healthz` (liveness, always 200, no auth).
  Readiness is also available at `/readyz` (200 healthy / 503 not).

## v0.5.1 — Patch Release

**v0.5.1** is a patch over v0.5.0. It adds two SSE endpoints the fusion-studio
bridge expects and hardens a few operational paths. No domain-behavior
regression; 543 tests green, ruff clean.

- **Scope-filtered SSE (#55)** — new endpoints
  `GET /api/v1/artifacts/{artifact_id}/events` and
  `GET /api/v1/sessions/{session_id}/events` let clients subscribe to a single
  artifact or session. They share `/api/v1/events/stream`'s handler; events
  whose `artifact_id` / `session_id` does not match are dropped server-side.
  The `kind` query filter remains combinable. Previously these paths 404'd and
  the studio client fell back to a local path.
- **Container image (#53)** — shipped `Dockerfile` + `.dockerignore`
  (`python:3.12-slim`, `.[otel]`, `/data` volume, `/healthz` HEALTHCHECK, ~262 MB).
- **start.sh status auth (#52)** — the health `ping` now sends `X-API-Key` so
  `status` no longer false-reports "not running" when auth is enforced.
- **Auto-release CI (#51)** — pushing a `v*` tag now auto-creates the GitHub
  Release with generated notes; prerelease/stable flag derived from the tag.
- **README English-only (#50)** — `README.md` contains zero CJK; Chinese lives
  in `README_CN.md`.

## Security (v0.3.11 audit hardening)

**v0.5.0** is the enterprise-grade production release. It closes the full audit
(§9 of the 2026-08-26 audit report) — all 10 P0 (released as v0.4.2-rc), 12 P1,
8 P2, and all MEDIUM/LOW findings. No domain-behavior regression; 538 tests green,
ruff clean. v0.5.0 headline remediation:

- **P1 (12)**: rate-limit production defaults + `rps=0` startup WARN; share
  `max_accesses` atomic CAS; per-request `X-Request-ID` (uuid4) + `LoggerAdapter`;
  periodic WAL checkpoint + online backup script; list pagination bounds
  (`page_size≤500`); `allow_no_auth` doc/impl alignment (fail-closed default);
  SSE max-lifetime + `kind_filter` validation; `save_artifact_and_version`
  two-phase write (tmp+rename); `future.cancel()` after request timeout;
  `export_session` error message sanitization; `cluster_node_id` field deletion
  + env-var override completion.
- **P2 (8)**: SSE thread isolation (separate SSE concurrency from RPC worker pool);
  RPC handler thread offload (all sync storage/engine calls via `asyncio.to_thread`);
  IDOR owner-check (`caller_user_id` enforcement); chunked content I/O + SSE event
  size cap; optional OTel tracing (`rpc.server.duration` + `db.storage.duration`);
  incremental token counting (prefix-sum truncation, patch reuse); metadata function
  indexes + keyset pagination; render.py coverage 63% → 88%.
- **LOW (1..5)**: share_id format precheck; log-injection sanitization (CWE-117);
  `/metrics` token auth; CSP nonce (CWE-79); LOW-2 documented-skip (runtime key
  rotation).

**v0.4.1** adds ops-integration test coverage on top of v0.4.0 — `tests/test_ops_integration.py`
(9 live-server tests) covers the do_POST/do_GET paths the v0.4.0 unit tests exercised only at the
handler level: JSON-RPC rate-limit `429` / `-32003` trigger, public-share rate-limit `429`,
`/metrics` Prometheus content + `404`-when-disabled, `/readyz` `503` on storage failure/exception,
and `artifact.inject`/`artifact.interact` JSON-RPC `-32005` at the HTTP layer. server.py coverage
77.4% → 80.0%, total 92.5%, **425 tests green**, ruff clean.

**v0.4.0** lands the ops-readiness batch — six production-operations capabilities added
on top of v0.3.11. No domain behavior change; 416 tests green, ruff clean. v0.4.0 highlights:

- **Ops-1 token-bucket rate limiting**: `RateLimiter` (token bucket) guards the default JSON-RPC path and the
  public share path with separate buckets, so unauthenticated share abuse cannot starve the
  authenticated backend. `rps=0` = unlimited (backward-compatible default). Over-capacity calls
  return JSON-RPC `-32003` (retryable) / HTTP `429`. See `rate_limiter.py`.
- **Ops-2 Prometheus /metrics**: `GET /metrics` emits Prometheus text exposition format 0.0.4
  (counters `rpc_requests_total` / `rpc_error_total`, gauge `rpc_active_conns`, histogram
  `rpc_request_latency_seconds`). Self-implemented, no `prometheus_client` dependency. See `metrics.py`.
- **Ops-3 log rotation + structured JSON**: `RotatingFileHandler` (10MB, 5 backups) with a
  `JsonFormatter` writes machine-parseable structured logs to `logs/` under the storage root
  (env override `FUSION_ARTIFACTS_LOG_DIR`); a human-readable console handler stays on stderr.
  Initialized at daemon startup in `__main__`. See `utils.py`.
- **Ops-4 disk-space check**: `shutil.disk_usage` pre-write check in `SQLiteStorage` rejects writes
  (raise `ResourceLimitError` `-32003`) when used-disk pct ≥ `storage.disk_space_warning_pct`
  (default 90 via `default_config.yaml`; field default 0 so unit tests stay decoupled from host
  disk state). The engine catches the error, publishes a `system.alarm` / `disk_full_alarm`
  event (severity critical) on the EventBus, then re-raises.
- **Ops-5 /healthz + /readyz split**: `GET /healthz` (liveness, always `200`, process-alive, no
  auth) vs `GET /readyz` (readiness, `200` ready / `503` not_ready, checks `storage.health_check()`
  + `event_bus.is_closed()`, no auth). Proper K8s probe separation — no restart loop on a
  transiently-unready dependency.
- **Ops-6 inject/interact removed**: `artifact.inject` / `artifact.interact` were advertised but
  never implemented (returned stubs). Commercial release must fail explicitly, not silently.
  RPC handlers now raise `-32005` `NotImplementedError` (REST `501`); the method names stay
  registered so old clients do not get `-32601` method-not-found. Engine-level
  `inject()` / `interact_artifact()` keep their signatures (internal API preserved). Error code
  range now: `-32001` NotFound / `-32002` Conflict / `-32003` ResourceLimit / `-32004` BusinessRule
  / `-32005` NotImplementedError.

**v0.3.11** patches the `start.sh` lifecycle script: `ensure_venv` now prefers the repo-root `.venv` (monorepo convention) over a stale project-local `.venv`, fixing `ping.version` reporting an outdated version. No engine change; 385 tests green, ruff clean.

v0.3.8 resolved all 59 findings from the 0824 security audit. v0.3.9 addressed the runtime/engineering findings (R1-R9, E1-E11) plus H2/H3/H6. **v0.3.10 completes the architecture refactors H5 and H7**, the two that materially change runtime behavior. H1 and H8 are retained by design (see below). 385 tests green, ruff clean. v0.3.10 highlights:

- **H5 event-loop thread offload**: blocking synchronous storage calls inside async engine methods now run via `asyncio.to_thread` (default ThreadPoolExecutor), so sqlite/file I/O no longer stalls the single event loop while a write holds the write-lock. Storage stays thread-safe (`check_same_thread=False` + `_write_lock` + read pool of 4).
- **H7 engine split**: the 1709-line `engine.py` god-class is split into modules behind a thin delegation layer (behavior-preserving) — `share.py` (`ShareManager` class with the R2 access buffer), `token_budget.py` (`context_budget`/`check_safety`/`inject`), and `section_index.py` (extended with `all_section_bounds`/`build_sections_with_tokens`/`replace_section`/`delete_section`). `engine.py` is now ~1300 lines and delegates.

### H1 / H8 — retained by design

- **H1 write-connection pool**: not added. SQLite WAL with `BEGIN IMMEDIATE` physically serializes writes, so a write-connection pool would be cargo-cult — the real write-amplification pain (file I/O under lock) was fixed by H2's two-phase write. A single write connection is architecturally correct.
- **H8 multi-node storage**: the `StorageDriver` ABC (47 abstract methods) is the swap point; actual multi-node replication belongs to the `fusion-multi-node` project, out of scope for this middleware.

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

The engine uses API key authentication via the `X-API-Key` header (v0.6.0: `Authorization: Bearer <key>` is also accepted — the fusion-gateway shell proxy injects Bearer tokens).

- If `api_key` is configured (env `FUSION_ARTIFACTS_API_KEY` or `security.api_key`), all requests must include `X-API-Key: <key>` or `Authorization: Bearer <key>` (constant-time compare)
- The gateway-issued `fm-fusion-artifacts-<hex>` service key persisted at `gateway.key_file` is also accepted as `Authorization: Bearer <key>` (v0.6.1, #82) — this is the credential the shell proxy injects
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
| `artifact.create` | session_id, name, type, content, summary?, change_log?, kind?, project_id?, metadata? | Create artifact + v1 |
| `artifact.get` | artifact_id, project_id? | Get metadata |
| `artifact.get_content` | artifact_id, version? | Get version content |
| `artifact.list` | session_id, include_deleted?, project_id?, metadata_filter? | List session artifacts |
| `artifact.delete` | artifact_id, soft_delete?, project_id? | Soft/hard delete artifact |
| `artifact.update` | artifact_id, content, change_log?, source?, expected_content_hash? | Create new version (optimistic lock) |
| `artifact.patch` | artifact_id, operation, anchor?, content?, expected_version? | Patch artifact (replace_section/append/prepend/delete_section) |
| `artifact.load` | artifact_id, preview_only?, section? | Load artifact (preview/section/full, per-section token counts) |
| `artifact.toc` | artifact_id, version? | Structured outline (anchor/level/line-range/chars/tokens), per-version cached |
| `artifact.view` | artifact_id, version?, section? \| offset?, limit? | Chunk read — O(section) or O(chunk), with totals for pagination |
| `context.budget` | session_id, context_window? | Session token budget summary |
| `artifact.auto_compact` | artifact_id, token_budget | Auto-compress artifact to fit token budget; returns `reason` on no-op (`already_within_budget`/`could_not_reduce`) |
| `artifact.version_diff` | artifact_id, from_version, to_version | Unified diff between two versions |
| `artifact.render` | content, session_id, lang_hint, project_id? | Auto-detect type, create renderable artifact |
| `artifact.check_safety` | messages, output_budget | Token budget safety check |
| `artifact.inject` | messages, output_budget | **Removed (Ops-6)**: raises `-32005` NotImplementedError; use `context.budget` + `check_safety` |
| `artifact.interact` | artifact_id, action, payload, session_id? | **Removed (Ops-6)**: raises `-32005` NotImplementedError; action dispatch not supported |
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
| `artifact.list_all` | filters?, sort?, page?, page_size?, cursor? | List all artifacts (cross-session); response includes `next_cursor` for keyset pagination (only `updated_at`/`created_at` sort) |

### Recycle Bin Methods (P1)

| Method | Params | Description |
|---|---|---|
| `artifact.list_recycle` | — | List soft-deleted artifacts |
| `artifact.restore` | artifact_id | Restore from recycle bin |
| `artifact.purge_expired` | — | Hard-delete expired recycled items |

### Share Methods (P1)

| Method | Params | Description |
|---|---|---|
| `artifact.create_share` | artifact_id, max_accesses?, expires_at?, permission? (view/remix) | Create share link (upserts permission on existing link); returns `share_url` (`/s/<share_id>`) |
| `artifact.get_shared` | share_id | Get shared artifact (public) |
| `artifact.revoke_share` | share_id | Revoke share link |
| `artifact.remix` | artifact_id, new_name? | Remix a derived copy with `parent_artifact_id` lineage |

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
| `artifact.list_events` | artifact_id?, session_id?, since_ts?, page?, page_size? | List events |

### Project KB Method (P3)

| Method | Params | Description |
|---|---|---|
| `artifact.move_to_project_kb` | artifact_id, project_id | Move artifact to project knowledge base |

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
GET /api/v1/share/{share_id}                         # Public share access (no auth; 400 if malformed share_id, 410 Gone if revoked/expired/exhausted, 404 if not found)
GET /api/v1/share-base                               # Authenticated: configured share page base URL (#83)
GET /s/{share_id}                                    # Public rendered share page (no auth; 410 expired page, 404 unknown) (#83)
GET /api/v1/events?artifact_id=...&session_id=...    # Read persisted events (REST twin of artifact.list_events; supports page/page_size) (#100)
```

`GET /api/v1/events` is the read-back endpoint for persisted events — distinct from the
SSE stream at `GET /api/v1/artifacts/{id}/events` (#55), which subscribes instead of listing.

`GET /api/v1/share/{share_id}` validates the `share_id` format (`shr_<12>`; LOW-1) before
hitting the DB — a malformed id returns `400` immediately, and a valid-but-absent id returns
`404` without leaking whether the share exists.

Query parameters for `GET /api/v1/artifacts`:
- `session_id` — scope list to a session; omitted → list all artifacts (paginated)
- `include_deleted` — include recycle-bin artifacts (true/false)
- `project_id` — scope to a project KB
- `metadata_filter` / `filters` — JSON query object passed to storage filtering
- `page` / `page_size` — pagination (list-all mode)
- `sort` — sort field (updated_at, created_at, name, starred)

**POST** endpoints:
```bash
POST /api/v1/artifacts           # {"session_id": "...", "name": "...", "type": "code", "content": "..."}  → 201 (collection-create, REST convention) (#100)
POST /api/v1/artifacts/create    # same as above (legacy alias)
POST /api/v1/artifacts/{id}      # {"action": "delete"} → delete; otherwise → artifact.update
```

All other mutations (rename, star, pin, duplicate, restore, move-to-kb, move-to-folder,
snapshot, share creation, tags, folders, events, purge, external-source create) are
**JSON-RPC only** — the REST surface intentionally covers artifact CRUD + public share;
see the method table above for the JSON-RPC method names (e.g. `artifact.create_share`).

### Ops Endpoints (Ops-5 / Ops-2)

Liveness/readiness probes and metrics, all **no-auth** (K8s probes carry no API key):

```bash
GET /healthz    # Liveness — process alive = 200 {"status":"ok","check":"liveness"}
GET /readyz     # Readiness — 200 {"status":"ready","checks":{...}} / 503 {"status":"not_ready",...}
                #   checks: storage (SELECT 1 + content_dir exists), event_bus (not closed)
GET /metrics    # Prometheus text exposition 0.0.4 — counters/gauge/histogram (Ops-2)
                #   404 if metrics.enabled=false
                #   401 if metrics.token set and X-Metrics-Token header missing/mismatched (LOW-4)
```

`/healthz` always returns 200 if the process can answer — it never depends on storage or the
event bus, so a transiently-unready dependency does not trigger a K8s restart loop. `/readyz`
returns 503 when storage is unreachable or the EventBus is shut down, signalling "do not route
traffic here yet". `/metrics` exposes `rpc_requests_total`, `rpc_error_total`, `rpc_active_conns`,
and `rpc_request_latency_seconds` (histogram, fixed buckets) for scraping.

**`/metrics` token (LOW-4)** — when `metrics.token` is set (env `FUSION_ARTIFACTS_METRICS_TOKEN`),
`/metrics` requires an `X-Metrics-Token` request header matching it (constant-time compare); a
missing or wrong header returns `401`. Empty/`""` (default) = no auth, relying on the `127.0.0.1`
bind for isolation. **If you expose the port beyond localhost, you must set `metrics.token`** to
prevent operational metrics leakage.

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

Scope to a single artifact or session (#55 — used by the fusion-studio bridge):
```bash
# Only events for one artifact (artifact.updated/deleted carry artifact_id)
curl -N http://127.0.0.1:11451/api/v1/artifacts/{artifact_id}/events

# Only events for one session (artifact.created carries session_id)
curl -N http://127.0.0.1:11451/api/v1/sessions/{session_id}/events
```
The `kind` query filter may be combined with either scope path. Events whose
`artifact_id` / `session_id` does not match are dropped server-side.

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
- **Single-tenant boundary**: the engine binds `127.0.0.1` and authenticates by a single shared `X-API-Key`. There is **no per-user identity** — all callers sharing the key are treated as one trusted principal. Do **not** expose the port beyond the host. Multi-tenant deployments must front the engine with an auth proxy that injects a trusted `caller_user_id`.
- **IDOR protection (v0.5.0)**: write ops and `artifact.get` enforce ownership when a `caller_user_id` is supplied in the RPC params. If `caller_user_id` is set and the artifact has an `owner_user_id`, they must match — otherwise `PermissionError` (`-32006`, HTTP `403`) is raised and the op is denied. When `caller_user_id` is omitted (single-tenant default) or the artifact has no owner set, the check is skipped for backward compatibility. Applies to: `artifact.get`, `artifact.get_content`, `artifact.update`, `artifact.patch`, `artifact.delete`, `artifact.version_rollback`. Ownership is assigned at creation via the optional `owner_user_id` / `ownership_type` (`free`/`project`/`cowork`) params on `artifact.create`.
- **share_id format validation (LOW-1)**: the public `GET /api/v1/share/{share_id}` endpoint rejects malformed share IDs (`shr_<12>` format) with `400` before any DB lookup — saving a round-trip and not leaking share existence.
- **CSP nonce, no `unsafe-inline` (LOW-5)**: rendered share HTML issues a one-time random `style-src 'nonce-<random>'` per render instead of `style-src 'unsafe-inline'`; the CSP header and every `<style>` tag share the same nonce, tightening the XSS surface on shared artifact previews.
- **Log injection guard (LOW-3)**: a global `LogSanitizerFilter` strips CR/LF from log messages and `%s` args, so user input (e.g. artifact names) cannot forge fake log lines (CWE-117).

## Backup & Restore (v0.4.2)

The daemon runs in SQLite **WAL** mode. A background thread performs a periodic `PASSIVE` checkpoint (default every 300s) to bound `-wal` growth, and `close()` runs a final `TRUNCATE` checkpoint so a stopped instance leaves a clean `meta.db` with no outstanding WAL frames.

**Online backup** — use `scripts/backup.sh` while the daemon is running (no lock, WAL-consistent snapshot):

```bash
# Default: ~/.fusion/artifacts -> ~/.fusion/artifacts-backup-<timestamp>
./scripts/backup.sh

# Custom destination
./scripts/backup.sh /var/backups/artifacts-20260826

# Override source storage root
STORAGE_ROOT=/data/artifacts ./scripts/backup.sh /backup
```

The script snapshots `meta.db` via `sqlite3 .backup` (WAL-consistent, does not block reads/writes) and incrementally rsyncs `content/`. Requires `sqlite3` and `rsync` on PATH.

**Restore** — stop the daemon, then copy the snapshot `meta.db` and `content/` back into the storage root:

```bash
./start.sh stop
rsync -a /var/backups/artifacts-20260826/meta.db ~/.fusion/artifacts/
rsync -a /var/backups/artifacts-20260826/content/ ~/.fusion/artifacts/content/
./start.sh start
```

**Config** — tune the checkpoint interval via `wal_checkpoint_interval` (seconds; `0` disables the background thread, relying on SQLite's default 1000-page auto-checkpoint):

```yaml
storage:
  wal_checkpoint_interval: 300   # env: FUSION_ARTIFACTS_WAL_CHECKPOINT_INTERVAL
```

**Metadata indexes (v0.5.0)** — `metadata_indexed_keys` lists high-frequency metadata filter field names. For each key, the engine creates a `json_extract(metadata, '$.<key>')` expression index so `metadata_filter` lookups hit the index instead of a full table scan. Keys must be safe identifiers (letters/underscore/digits); invalid keys are dropped. Empty list = no indexes (backward-compatible default).

```yaml
storage:
  metadata_indexed_keys: ["language", "framework"]
```

**Keyset pagination (v0.5.0)** — `artifact.list_all` accepts an optional `cursor` (opaque, returned as `next_cursor` in the response). When supplied with `updated_at` or `created_at` sort, the engine uses a `WHERE (sort_col, id) < (cursor)` keyset query instead of `OFFSET`, avoiding the deep-page scan-and-discard cost. Other sort modes and invalid cursors fall back to OFFSET pagination.

**Incremental token counting (v0.5.0)** — `auto_compact` and `patch_artifact` avoid redundant full-encoding of large content:
- The compactor truncation loop computes per-line token counts once and walks a prefix sum, so each section boundary is evaluated in O(1) instead of re-joining and re-encoding the whole string (O(n²) previously). Intermediate "under budget?" checks are gated by a cheap `estimate_tokens` heuristic and only call the exact encoder on candidates.
- `patch_artifact` reuses the persisted `version.token_count` for old/new content instead of re-counting (3 encodes → 1).
- `auto_compact` runs compression + counting off the event loop via `asyncio.to_thread`, so 1MB+ content does not block other requests.

**Chunked content I/O (v0.5.0)** — large version content (>10KB, stored on disk) is written and read in 1MB chunks instead of via `write_text`/`read_text`, which load the entire string into memory. This bounds peak I/O memory to the chunk size regardless of content size, preventing the 1.9–2.5GB OOM peak under 64 concurrent 10MB requests. Disk-full/ENOSPC during a chunked write maps to `ResourceLimitError` and cleans the partial file.

**SSE event size cap (v0.5.0)** — `sse.max_event_bytes` bounds the serialized size of a single SSE event (default 256KB). Events exceeding the cap are dropped with a warning log instead of being written, so one large payload cannot block a connection thread or balloon client buffers. `0` disables the cap (backward-compatible). Env override: `FUSION_ARTIFACTS_SSE_MAX_EVENT_BYTES`.

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
  disk_space_warning_pct: 90   # Ops-4: reject writes (ResourceLimitError) + alarm when used-disk ≥ pct

thresholds:
  auto_create_lines: 30
  auto_create_chars: 1500

artifact:
  id_prefix: "art_"

security:
  allow_no_auth: false   # fail-closed default; set true only for trusted single-user local use
  recycle_retention_days: 7

share:
  public_base_url: ""    # #83: public base for /s/<share_id> links (empty → http://host:port)

gateway:                 # #75/#82 web-shell integration
  enabled: false
  url: "http://127.0.0.1:11432"
  module_id: "fusion-artifacts"
  admin_key: ""          # bootstrap only; fm- service key persisted after first register
  key_file: ""           # default <storage_root>/gateway_service_key (mode 600)
  heartbeat_interval: 15

ipc:                     # #84 fusion-design / v0.7.0 fusion-office save_artifact consumer
  enabled: true
  design_dir: ""         # default <FUSION_DESIGN_DATA_DIR or ~/.fusion-design>/fusion-artifacts-engine
  office_enabled: true   # v0.7.0 office ingest + .outbox write-back
  office_dir: ""         # default <FUSION_OFFICE_DATA_DIR or ~/.fusion-office>/fusion-artifacts-engine
  poll_interval: 3.0

long_context:            # v0.7.1 #91 rolling summary + rag archival (compactor stays last-resort)
  summary_threshold: 5   # versions before mlx summary kicks in; 0=disabled
  summary_model: ""      # empty = mlx default model
  mlx_url: "http://127.0.0.1:11434"
  mlx_api_key: ""        # empty = FUSION_MLX_API_KEY env
  rag_enabled: true
  rag_url: "http://127.0.0.1:11436/kb"   # /kb prefix = fusion-rag app-level route
  rag_api_key: ""        # empty = FUSION_RAG_API_KEY env
  rag_kb_id: "fusion-artifacts"

rate_limit:              # Ops-1: token bucket (rps=0 / burst=0 = unlimited, backward-compat default)
  rps: 0                 # default JSON-RPC bucket
  burst: 0
  public_rps: 0          # public share bucket (separate so share abuse can't starve backend)
  public_burst: 0

metrics:                 # Ops-2
  enabled: true          # false → GET /metrics returns 404

sse:
  heartbeat_interval: 30
  max_lifetime: 3600        # v0.4.2: max seconds per SSE connection; 0=unlimited. Server closes stream after expiry (emits __max_lifetime__) to force client reconnect; prevents zombie long-lived connections holding worker threads
  max_event_bytes: 262144   # v0.5.0: max serialized bytes per SSE event; oversized dropped + warned. 0=unlimited
  max_connections: 16       # v0.5.0: SSE concurrency cap (separate from server.max_workers). On SSE handshake the connection releases its RPC worker slot and takes a slot from this dedicated semaphore; over-cap SSE gets 503. 0=legacy mode (SSE keeps worker slot, no separate cap; not recommended)
```

**Environment variables (v0.5.5)** — env overrides file values (highest precedence, after CLI flags). Contract names per `architecture/config-conventions.md` §1, resolved via `fusion_core.config` single-point parsing when available (in-repo fallback keeps behavior identical):

- `FUSION_ARTIFACTS_PORT` — listen port (invalid value logs a warning and keeps the YAML value)
- `FUSION_ARTIFACTS_DATA_DIR` — storage root, `~` expanded; wins over the deprecated `FUSION_ARTIFACTS_STORAGE_ROOT` alias when both are set (setting only the alias logs a deprecation warning)
- `FUSION_ARTIFACTS_CONFIG` — path to user config YAML (default `~/.fusion/artifacts/config.yaml`)
- `FUSION_ARTIFACTS_HOST`, `FUSION_ARTIFACTS_API_KEY`, plus per-field ops overrides (`FUSION_ARTIFACTS_MAX_WORKERS`, `FUSION_ARTIFACTS_RATE_LIMIT_RPS`, `FUSION_ARTIFACTS_SSE_*`, `FUSION_ARTIFACTS_TRACING_*`, …)

See `config.example.yaml` for a full annotated reference. Env precedence: contract vars (`PORT`/`DATA_DIR`) apply after legacy vars, so `DATA_DIR` > `STORAGE_ROOT` > YAML.

**Process title (v0.5.5)** — the daemon sets its process title to `fusion-artifacts-engine serve :<port>` so `ps` / Activity Monitor show the product name instead of a bare `Python`. Requires `setproctitle` (install via `pip install 'fusion-artifacts-engine[proc]'`, included in `[all]`); when absent the title rename is skipped silently and the daemon runs unchanged — no functional impact. `start.sh` tracks the daemon by PID file, not by process name, so the rename cannot break start/stop/status.

**Port registry self-check (v0.5.6, #72)** — before launching, `start.sh start` verifies that the port it is about to serve (from `FUSION_ARTIFACTS_PORT`, default 11451) is registered for `fusion-artifacts-engine` in `architecture/port-registry.yaml` (the monorepo single source of truth for ports), per Config Phase 3.3 register-then-start. Behavior:

- Port not found under the service entry → **refuses to start** (exit 1) with an error naming the port and registry path
- Registry file missing → warns and continues (standalone deployments without the workspace docs are normal)
- `FUSION_SKIP_REGISTRY_CHECK=1` → warns and bypasses the check (escape hatch for isolated-port testing)
- `FUSION_REGISTRY=/path/to/registry.yaml` → point the check at an alternate registry copy (defaults to `~/fusion/architecture/port-registry.yaml`)

**SSE thread isolation (v0.5.0)** — `sse.max_connections` separates SSE long-lived connections from the RPC worker pool. Previously an SSE connection held its `server_max_workers` slot for its entire lifetime, so 64 concurrent SSE clients exhausted all 64 RPC worker threads and every new RPC request got `503`. Now the SSE handshake acquires a slot from a dedicated `_sse_sem` (size `max_connections`, default 16) and **releases** the RPC worker slot back to the pool, so RPC stays available no matter how many SSE clients are connected. Client disconnect is detected within ~1s via a non-blocking socket probe (not waited out to the next heartbeat, which at the default 30s would delay slot release and let a connect/disconnect churn client exhaust the cap). `max_connections=0` falls back to the legacy single-pool behavior. Env override: `FUSION_ARTIFACTS_SSE_MAX_CONNECTIONS`.

**RPC handler thread offload (v0.5.0)** — async RPC handlers previously called synchronous `engine`/`storage` methods directly (`self.engine.get_artifact`, `self.engine.storage.list_artifacts`, …). Each call ran on the single event loop and blocked every other request while it hit SQLite or the filesystem. Every synchronous storage/engine call in `rpc/methods.py` now runs via `await asyncio.to_thread(...)` on the default ThreadPoolExecutor, so the event loop only schedules — sqlite/file I/O no longer stalls it. `_publish` is async (its kind look-up read is offloaded too), so SSE event emission never blocks the loop. Async engine methods (`create_artifact`, `update_artifact`, …) were already non-blocking and are awaited directly. Verified by `tests/test_p2_2_to_thread_offload.py` (read/write offload + worker-thread execution + async publish).

**OTel tracing (v0.5.0)** — optional OpenTelemetry integration emitting `rpc.server.duration` (per dispatch, attribute `rpc_method`) and `db.storage.duration` (per storage op, attribute `db_operation`) spans, so the causal chain RPC → SQLite is observable end-to-end. **Off by default** — this is a local-first single-tenant daemon, and OTel is an optional dependency, not a hard one. Three states:

- `opentelemetry-api`/`opentelemetry-sdk` **not installed** → `tracing.span`/`traced` are pure no-ops (zero overhead, zero import error). Install via `pip install 'fusion-artifacts-engine[otel]'`.
- installed + `tracing.enabled: false` (default) → no-op tracer.
- installed + `tracing.enabled: true` → real tracer. Default exporter is `ConsoleSpanExporter` (sufficient for local single-node). For a collector backend, set `OTEL_EXPORTER_OTLP_ENDPOINT` and the OTel SDK auto-switches to OTLP — no code change needed.

Config (in `default_config.yaml`, overridable via `~/.fusion/artifacts/config.yaml` or env `FUSION_ARTIFACTS_TRACING_ENABLED` / `FUSION_ARTIFACTS_TRACING_SERVICE_NAME`):

```yaml
tracing:
  enabled: false                 # default off; set true to emit spans
  service_name: "fusion-artifacts-engine"
```

`configure_tracing()` runs once at startup; `shutdown()` flushes the span processor on graceful exit. OTel context propagates across `asyncio.to_thread` via contextvars, so spans nest correctly even when storage calls are offloaded (P2-2). Verified by `tests/test_p2_5_otel_tracing.py` (no-op path + enabled rpc/db spans + error-span status + exception propagation + decorated write-path span).

## Architecture

```
Product Layer: Fusion Code (tool_use) | Fusion Studio (GUI)
---------------------------------------------------------
Middleware: Artifacts Engine (Python daemon, JSON-RPC 2.0 + REST /api/v1 + SSE)
  engine.py (domain core, ~1300 lines, delegates to modules)
    ├─ share.py        ShareManager — share access control + R2 access buffer
    ├─ token_budget.py context_budget / check_safety / inject
    ├─ section_index.py section bounds + patch (replace/delete section)
    ├─ render.py       share HTML render (lxml Cleaner + permission footer)
    ├─ gateway_client.py shell manifest self-registration + fm- service key lifecycle
    ├─ ipc_consumer.py fusion-design / fusion-office save_artifact file-IPC watcher
    ├─ office_ingest.py office (docx/xlsx/pptx) → sectioned markdown parse + write-back
    └─ auto_identifier.py should_create_artifact (threshold + renderable-type)
  async engine methods offload blocking storage calls via asyncio.to_thread
---------------------------------------------------------
Ops layer (v0.4.0):
  rpc/server.py  ─ /healthz (liveness) / /readyz (readiness) / /metrics (Prometheus)
  rate_limiter.py ─ token bucket (default + public share buckets), 429 / -32003
  metrics.py     ─ Prometheus text exposition 0.0.4 (counters/gauge/histogram)
  utils.py       ─ RotatingFileHandler + JsonFormatter (10MB/5 backups, structured JSON)
  storage        ─ disk_usage pre-write check → ResourceLimitError + disk_full_alarm event
---------------------------------------------------------
Storage: SQLite (WAL, single write conn + read pool of 4) + Filesystem
  StorageDriver ABC (47 abstract methods) — swap point for multi-node/Postgres
```

## React Rendering Layer (v0.5.3, issue #63)

`client/` ships `@fusion/artifacts-visual` — the React rendering layer for the
Socratic DSL v3.0 visual engine (PRD k12-train-model A8). It mirrors the
server-side Python compilers in `fusion_artifacts_engine/visual_primitives/`:

- **8 primitive components** (`src/primitives/`): `ArrayGrid`, `TapeDiagram`,
  `Geometry2D`, `Isometric3D`, `TrackTimeline`, `BucketDivider`, `DataChart`,
  `FlowCard` — dependency-free SVG (the server-side `isometric_3d` uses
  Three.js and `data_chart` uses Chart.js via CDN; the React layer draws both
  natively in SVG so it stays SSR-safe and dependency-free).
- **`VisualEngineDispatcher`** — 4-level fallback chain: primary
  (`meta.visual_type`) → `meta.fallback_type` → `TapeDiagram` → `FlowCard`,
  each level wrapped in an `ErrorBoundary` with a final static error card
  (100% render guarantee, mirrors `visual_dispatcher.py` +
  `visual_error_boundary.py`).
- **`useVisualStream`** — consumes the finite SSE stream
  `GET /api/v1/render-visual/stream` via `fetch` + `ReadableStream`
  (header-based `X-API-Key` auth; `EventSource` cannot set headers), parsing
  `render_step` / `render_final` / `render_error` events.
- **`VisualStage`** — top-level component: local mode (`dsl` + `progress`) or
  streamed mode (`rawText` / `dslJson` → SSE).

```tsx
import { VisualStage, VisualEngineDispatcher } from "@fusion/artifacts-visual";

// Local: DSL + progress, dispatcher runs client-side.
<VisualStage dsl={dsl} progress={0.5} />

// Streamed: raw model output, server parses + renders per pipeline step.
<VisualStage rawText={modelOutput} baseUrl="http://127.0.0.1:11451" apiKey={key} />
```

Tests: `cd client && bun install && bun test` (16 tests), typecheck:
`bunx tsc --noEmit`.

## Web Shell Integration (v0.6.0, issue #75)

The daemon self-registers as a fusion-gateway web-shell module at startup, so the 📦 **Artifacts** menu appears in the union web shell (`http://127.0.0.1:11432`) with zero gateway-side config changes.

```yaml
# ~/.fusion/artifacts/config.yaml
security:
  api_key: "<strong-key>"      # direct-caller credential (fusion-studio / fusion-code)
gateway:
  enabled: true
  url: "http://127.0.0.1:11432"
  module_id: "fusion-artifacts"
  admin_key: "<gateway master/admin key>"  # bootstrap only
  key_file: ""                 # default: <storage_root>/gateway_service_key (mode 600)
  heartbeat_interval: 15       # keep below gateway heartbeat_timeout (30s)
```

- **Self-registration**: on start the engine POSTs its shell manifest to `POST /api/v1/shell/manifests` (id `/shell/fusion-artifacts/`, health `/healthz`, upstream `http://127.0.0.1:11451`). The body **omits `api_key`**, so the gateway issues an `fm-fusion-artifacts-<hex>` service key and returns the plaintext exactly once (`raw_key`). The first bootstrap authenticates with `gateway.admin_key`; afterwards the stored service key authenticates re-registration.
- **Service key persistence** (#82): `raw_key` is written atomically to `gateway.key_file` (default `<storage_root>/gateway_service_key`, mode 600). Heartbeats use it — never the admin key — so steady-state operation makes no admin-plane calls and the log shows a steady heartbeat cadence.
- **Heartbeat**: a daemon thread POSTs `…/shell/manifests/fusion-artifacts/heartbeat` every 15s. `404 manifest_unknown` (gateway restart with a wiped store) or `403 heartbeat_forbidden` (no matching service-key record) triggers one re-registration; the gateway issues a fresh key, which is persisted and used from then on. Re-registration is rate-limited to once per 5 minutes, so a permanently revoked key cannot produce a 15s re-register loop.
- **Proxy auth**: the gateway shell proxy strips caller credentials, forwards `/shell/fusion-artifacts/<rest>` to `upstream/<rest>`, and injects `Authorization: Bearer <fm- service key>`. The engine's `_is_authed` accepts both its configured `api_key` (X-API-Key / Bearer) and the persisted service key (Bearer), matching the gateway #294 contract that modules validate the issued key.
- Registration/heartbeat failures are **non-fatal**: the daemon keeps serving even if the gateway is down.

## Web GUI (v0.6.0, issue #76; v0.7.3 shell-spec alignment; v0.7.4 create tiles; v0.7.5 editor + AI; v0.7.6 svg/mermaid)

A built-in web GUI (`webui/`, Vite + React 18 + Tailwind v4) served by the daemon itself from `fusion_artifacts_engine/webui_dist/` — same-origin at `http://127.0.0.1:11451/` and through the shell proxy at `/shell/fusion-artifacts/`. Kernel zero change: the GUI consumes only existing JSON-RPC methods.

- **Library view** (v0.7.3 redesign; v0.7.4 create tiles) — claude.ai-style time sections (Today / Yesterday / This week / Earlier) replace the v0.6.3 per-session group headers (raw session ids were unreadable and fragmented the grid); segmented-control type tabs (All / HTML apps / Documents / Charts / Code) with counts, artifact/version stat header, toolbar row carrying name+summary search, sort (updated/created/name — name sort degrades to a flat list) and the recycle bin chip with an expandable panel and restore. Three create tiles sit above the grid — new document (markdown starter), new PPT (self-contained HTML slide deck with keyboard/click nav), new design (JSON canvas) — each creates via `artifact.create` (session `webui-created`) and opens the detail view; template names follow the shell language. Skeleton loading state, empty state with guidance copy. Cards carry live preview thumbnails (sandboxed script-free iframe for html/react/svg, rendered markdown, icon tile for JSON/data payloads, text snippet otherwise — lazily fetched on scroll and cached), type-tinted chips, starred indicator, source attribution (`source_module` → `metadata.source` → session), shared/remix badges, rolling-summary excerpt.
- **Detail view** — three-state preview: HTML/React renders in a sandboxed iframe (`sandbox="allow-scripts allow-forms allow-popups"`, blob URL — no inline echo of artifact bytes), markdown renders via `marked`, code/data shown as text; version timeline with compare (unified diff, color-coded) and one-click rollback; referencing-sessions reverse lookup from persisted events; share create/revoke/copy-link. Restore/remix confirmations use an in-app modal (no native `confirm`, spec S6).
- **Content editor (v0.7.5, issue #96)** — the Edit button opens a code-mode textarea for every artifact type (markdown/html/code/data — so the v0.7.4 PPT and design tiles produce artifacts the GUI can edit, issue #98); Save calls `artifact.update` with `expected_content_hash` (optimistic locking — a `-32002` conflict surfaces an explicit "changed elsewhere" error, never a silent overwrite) and lands as a new version with `source=manual`.
- **AI generation & iteration (v0.7.5, issue #97)** — new `ai.generate` / `ai.list_models` RPC methods: a thin server-side proxy to fusion-mlx's OpenAI-compatible `/v1/chat/completions` (no CORS, engine auth applies). Config section `ai` (`base_url` default `http://127.0.0.1:11434`, `model` empty = first entry of `/v1/models`, `timeout`, `api_key`). The GUI exposes it two ways: an "AI edit" card in the detail view (prompt → model rewrites the full content → new version with `source=ai_generation`, model picker populated from `ai.list_models`) and an "AI generate" tile in the library (prompt + type select → generates markdown/html/code/data → creates the artifact and opens it). Model responses are fence-stripped client-side; backends are pinned per deployment via `ai.model` (auto-pick grabs whichever model is first, which may be an image model).
  ```json
  {"jsonrpc": "2.0", "id": 1, "method": "ai.generate",
   "params": {"prompt": "把标题改成秋季版", "system": "输出完整新内容，不要解释", "model": "mlx-community/Qwen3.5-4B-MLX-4bit"}}
  {"jsonrpc": "2.0", "id": 2, "method": "ai.list_models", "params": {}}
  ```
- **Shell integration (v0.7.3, `fusion-web-ui-spec-1015` S7/S8/S9)** — when served under `/shell/` (or `?embedded=1`) the module hides its own top nav and follows the host shell: theme from `fusion.shell.theme` + `fusion:theme` events, language from `fusion.shell.lang` + `fusion:lang` events (same-origin storage bridge until `@fusion/web-sdk` ships). Standalone mode keeps local toggles. Visual language follows the spec: `rounded-lg` cards / `rounded-md` controls, borders over shadows on flat surfaces, Lucide icons at 16/20/24, 4-multiple spacing, dark theme default.
- Static serving hardening: entry served `no-store` with a strict CSP (`frame-ancestors` allows the gateway origin so iframe embedding works), hashed assets served `immutable`, path traversal rejected, missing dist degrades to 404 without touching RPC.

```bash
# Rebuild the GUI after editing webui/
cd webui && bun install && bun run build   # output → fusion_artifacts_engine/webui_dist/
```

The compiled dist is committed as package-data (CI test `test_webui_real_dist_shipped` fails if it's missing).

## Share Permissions & Remix (v0.6.0, issue #77)

Claude-Artifacts-style remix + share, scoped to the intranet:

- **Share permission levels** — `artifact.create_share` accepts `permission: "view" | "remix"` (default `view` = read-only). Creating a share for an artifact that already has one **upserts the permission on the same link** — changes take effect on the next open. The public payload `GET /api/v1/share/{share_id}` includes `permission`; expired/revoked/exhausted links return explicit `410 Gone` states.
- **Remix lineage** — `artifact.remix` copies the current version into a new independent artifact with `parent_artifact_id` set (`storage.duplicate_artifact(..., parent_artifact_id=...)`). Schema columns `artifacts.parent_artifact_id` and `artifact_shares.permission` are added by idempotent migrations. The GUI shows a remix badge on cards, a "remix of `<parent>`" lineage link in the detail header, and a Remix button that deep-links into the new copy.
- **[Continue conversation]** — the GUI detail view links to `/shell/fusion-cowork/?artifact_ref=<artifact_id>` (lightweight ~30-token ref; fusion-cowork consumes the reference, tracked cross-repo).

```json
{"jsonrpc": "2.0", "id": 1, "method": "artifact.create_share",
 "params": {"artifact_id": "art_x", "permission": "remix", "expires_at": "2026-11-09T00:00:00+08:00"}}
{"jsonrpc": "2.0", "id": 2, "method": "artifact.remix", "params": {"artifact_id": "art_x"}}
```

## Public Share Page (v0.6.1, issue #83)

Share links now open a rendered read-only page instead of a JSON payload:

- **`GET /s/<share_id>`** — unauthenticated public page (public rate-limit bucket). Markdown/HTML/SVG/data content is rendered through the existing sanitizer (lxml Cleaner + per-render nonce CSP), so the recipient sees the artifact, not `{"status":"ok",...}`.
- **Permission-aware**: `view` shares render read-only. `remix` shares render the same page plus a **Remix to my library** button that deep-links to the shell route `#/s/<share_id>?remix=1` — the shell prompts for login, then the GUI remixes the share into the viewer's library with `parent_artifact_id` lineage preserved.
- **Expired/revoked/exhausted** links render a clean expired page with `410 Gone`; unknown ids return `404`; the artifact body is never emitted in either case.
- **Link generation**: `artifact.create_share` now returns `share_url` (`<share.public_base_url>/s/<share_id>`); the GUI copy button uses it. `GET /api/v1/share-base` (authenticated) exposes the configured base for client-side link building. `share.public_base_url` defaults to `http://<server.host>:<server.port>` — set it when the daemon sits behind a reverse proxy or on a different public host.
- **v0.6.2 hardening (#86)**: the nonce CSP meta is emitted from one place in the page wrapper, so the `data` and code-fallback branches carry it too (previously only html/markdown/svg did); when the `share-base` fetch fails the GUI copy fallback is the same-origin `/s/<id>` page route, never the raw `/api/v1/share/` JSON endpoint; the logged-in remix deep link is covered end to end by `tests/test_remix_deep_link_e2e.py` (page → public share JSON → authenticated remix with lineage) plus a live browser run through the shell session gate.

## fusion-design Save-to-Artifacts IPC (v0.6.1, issue #84)

The engine consumes the frozen file-IPC contract produced by fusion-design's **[Save to Artifacts]** button (`fd-ecosystem EcosystemLink::send`; upstream dahai80/fusion-design#33):

- **Watch directory**: `<FUSION_DESIGN_DATA_DIR or ~/.fusion-design>/fusion-artifacts-engine/*.json` (override with `ipc.design_dir`; polling interval `ipc.poll_interval`, default 3s).
- **Message**: `{"target":{"Custom":"fusion-artifacts-engine"},"action":"save_artifact","payload":{"source":"fusion-design","title":...,"session":...,"version":...,"document":"<PenDocument JSON>"}}`.
- **Ingestion**: one artifact per design `session` (`session_id=ext_fusion-design_<session>`, type `data`, `source_module=fusion-design`, metadata `design_session`/`design_version`). A higher `version` appends a new artifact version; the same or lower version is a no-op (restart-safe idempotency). The Library card shows source **Design**.
- **File lifecycle**: handled messages move to `.processed/`; unparseable messages are retried 3 polls then moved to `.failed/`; oversized messages (>8MB) are rejected to `.failed/`. Nothing is re-ingested after restart.
- Disable with `ipc.enabled: false` (or `FUSION_ARTIFACTS_IPC_ENABLED=false`).

## fusion-office File Ingest & Write-back (v0.7.0)

The engine consumes office documents from fusion-office through the same file-IPC pattern as the design integration, turning binaries into long-context-friendly markdown — and writes patched content back as regenerated office files:

- **Read path (ingest)**: watch directory `<FUSION_OFFICE_DATA_DIR or ~/.fusion-office>/fusion-artifacts-engine/*.json` (override with `ipc.office_dir`; same `ipc.poll_interval`). Message contract: `{"target":{"Custom":"fusion-artifacts-engine"},"action":"save_artifact","payload":{"source":"fusion-office","title":...,"session":...,"version":...,"file_path":"<absolute path to .docx/.xlsx/.pptx>"}}` — the office binary is referenced by path, never inlined in JSON.
- **Parsing**: `docx` (paragraphs, `Heading N` styles → markdown `#`*(N+1), tables → pipe rows), `xlsx` (each sheet → `## <sheet title>` + a markdown table, first row as header, capped at 500 rows/64 cols per sheet with a truncation note, empty sheets render `(empty sheet)`), `pptx` (`## Slide N` + text runs). Unsupported suffixes fail the message to `.failed/`.
- **Section addressing for free**: because content is markdown with `#` headings, the existing `section_index` pipeline addresses sections directly (docx headings, xlsx sheets, pptx slides) — patch/budget/reference flows are untouched.
- **Idempotency**: one artifact per office `session` (`session_id=ext_fusion-office_<session>`, type `markdown`, `source_module=fusion-office`, metadata `office_format`/`office_session`/`office_version`/`office_source_path`/`office_stats`). Higher version appends; same/lower skips. Library card shows source **Office**.
- **Write-back**: after `artifact.update` or `artifact.patch` on an office-sourced artifact, the engine regenerates the office binary from the patched markdown (`markdown_to_docx` / `markdown_to_xlsx`, cells coerced back to numeric types) and exports it to `<office_dir>/.outbox/<artifact_id>_v<N>.<fmt>` for fusion-office to pick up. **The engine never overwrites the user's original file.** `pptx` write-back is explicitly unsupported (`NotImplementedError`) — read-only for pptx. Write-back failures are logged only; they never fail the RPC result.
- **Config** (`ipc` section): `office_enabled: true`, `office_dir: ""` (empty = derived default). Env overrides: `FUSION_ARTIFACTS_IPC_OFFICE_ENABLED`, `FUSION_ARTIFACTS_IPC_OFFICE_DIR`.

```json
{"jsonrpc": "2.0", "id": 1, "method": "artifact.patch",
 "params": {"artifact_id": "art_x", "operation": "replace_section",
            "anchor": "Findings", "content": "### Findings\n\npatched body"}}
# → .outbox/art_x_v2.docx regenerated with the patched section
```

## Long-Context Read Path & Hierarchical Compression (v0.7.1, issues #90/#91)

Two long-context mechanisms layered on top of the existing compactor (which stays as the last-resort budget guard):

**Read path (#90)** — consumers no longer choose between a ~30-token ref and a full-content `get`:

- **`artifact.toc`** — structured outline per version: `{anchor, level, start_line, end_line, lines, chars, tokens}` (1-based, markdown headings / code defs). Cached per `(artifact_id, version)` (LRU 128) — a new version is a new cache key, so invalidation is automatic.
- **`artifact.view`** — chunk read: `section` (returns only the matched section body with `start_line`/`end_line`/totals) or `offset`+`limit` line-range (with `total_lines`/`total_bytes`/`truncated` for pagination). O(section), not O(artifact) — a multi-MB artifact costs one section's bytes per read. Out-of-bounds offsets return an empty chunk with totals; missing sections raise `-32001`.

```json
{"jsonrpc": "2.0", "id": 1, "method": "artifact.toc", "params": {"artifact_id": "art_x"}}
{"jsonrpc": "2.0", "id": 2, "method": "artifact.view", "params": {"artifact_id": "art_x", "section": "Findings"}}
{"jsonrpc": "2.0", "id": 3, "method": "artifact.view", "params": {"artifact_id": "art_x", "offset": 40, "limit": 80}}
```

Consumer contract (cowork/design/code): prompt assembly should carry the ref + TOC, and load chunks on demand via `artifact.view` instead of full `artifact.get`.

**Hierarchical compression (#91)** — background tasks after every `artifact.update`/`artifact.patch` (never block the RPC response; failures log only):

- **Rolling summary**: when an artifact's version count reaches `long_context.summary_threshold` (default 5), the engine asks fusion-mlx (`/v1/chat/completions`, ≤120 words) to summarize the version-change digest, writes it to `artifact.summary` (so every subsequent `ref_text` carries it), and stamps `metadata.summary_version` for per-version idempotency. Threshold `0` disables.
- **RAG archival**: when a new version supersedes the current one, the superseded version's content is pushed to fusion-rag (`POST /bases/{kb}/documents/ingest`, `doc_name=<artifact_id>_v<N>.txt`, metadata `artifact_id`/`artifact_version`/`artifact_type`). `metadata.archived_versions` guarantees one archive per version. fusion-rag unreachable/4xx → warning log, version creation unaffected. Old versions stay queryable: `POST /bases/fusion-artifacts/search` filtered by `artifact_id`.
- **Config** (`long_context` section): `summary_threshold` (5), `summary_model` ("" = mlx default), `mlx_url`, `mlx_api_key` ("" = `FUSION_MLX_API_KEY`), `rag_enabled` (true), `rag_url` (`http://127.0.0.1:11436/kb` — the `/kb` prefix is fusion-rag's app-level route), `rag_api_key` ("" = `FUSION_RAG_API_KEY`), `rag_kb_id` (`fusion-artifacts`). Env overrides: `FUSION_ARTIFACTS_LC_SUMMARY_THRESHOLD`, `_LC_RAG_ENABLED`, `_LC_MLX_URL`, `_LC_RAG_URL`.

## Artifact Card Deep-Link Contract (issue #78)

For cowork run timelines (fusion-cowork#224) and code session streams (fusion-code#228) rendering artifact reference cards:

- **Card data**: `artifact.get` / `GET /api/v1/artifacts/{id}` returns all card fields (name, type, kind, current_version, summary, source_module, is_starred). Cards stay fresh: re-fetch on render; version bumps appear on next render.
- **Conversation payload stays a lightweight ref**: `ref_text` format `[Artifact: <name> | ID: <id> | Version: v<n> | Type: <type> | Size: <bytes> | Summary: …]` (~30 tokens, no content inflation).
- **Deep link**: open `http://<shell-host>:11432/shell/fusion-artifacts/#/a/<artifact_id>` (hash route straight into the AR2 detail view; works cross-iframe from any shell module).

## Acceptance Parity Fixes (v0.7.6, issue #100)

Four defects found by the 2026-11-03 acceptance run, fixed end to end:

- **SVG / Mermaid artifact types (#100-1)** — `artifact.create` accepted `type=svg|mermaid` at the
  handler layer but pydantic and the SQLite `CHECK` constraint rejected the insert. All three layers
  now agree: `ArtifactType` literal, `_TYPE_TO_KIND` (`svg` → app, `mermaid` → document), and the
  storage DDL. Existing databases are migrated at startup (`migrate_artifact_type_check` rebuilds the
  `artifacts` table with the relaxed CHECK and re-creates every index from its captured DDL —
  idempotent, recorded in `applied_migrations`). The GUI renders both in the sandboxed preview iframe
  (Mermaid is bundled from the npm package and rendered to a static SVG in the module context — the
  preview iframe carries zero scripts, so CSP and offline/local-first both hold) and shows them in
  the correct library tabs/tiles.
- **Event persistence (#100-2)** — `artifact.created` / `artifact.updated` / rollback events were only
  published to the in-memory SSE bus, so `artifact.list_events` returned empty after any mutation and
  the reverse session-lookup stayed blank. `_publish` now also persists each event via
  `engine.emit_event` (non-fatal on failure, oversize payloads rejected with `-32003`).
- **REST collection create + event read (#100-3)** — `POST /api/v1/artifacts` (collection path,
  REST-convention create, `201`) is now a first-class alias of `/create`; `GET /api/v1/events?artifact_id=`
  reads persisted events back (REST twin of `artifact.list_events`, paginated). It deliberately uses the
  collection path because `/api/v1/artifacts/{id}/events` is the artifact-scoped SSE stream (#55).
- **Version id leak (#100-4)** — `ArtifactVersion.id` is the global SQLite rowid; exposing it leaked
  total-version counts and made unrelated artifacts' version ids jump ("another tenant's v1 has
  id=49"). The field is now excluded from every `model_dump()`; the API identifies versions by
  `(artifact_id, version_num)` as before.

Covered by `tests/test_acceptance_100.py` (713 tests green).

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

Current coverage across 425 tests (v0.4.1).

## License

Apache License 2.0

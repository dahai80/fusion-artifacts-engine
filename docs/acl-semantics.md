---
doc_id: fusion-artifacts-engine/acl-semantics
product: fusion-artifacts-engine
class: app
entry: service
doc_type: guide
title: { zh: "ACL 与多租户语义（客户端契约）", en: "ACL & Multi-tenant Semantics (Client Contract)" }
audience: engineer
status: draft
version: 0.7.21
updated: 2026-11-28
owner: "@dahai"
source_of_truth:
  - fusion_artifacts_engine/rpc/methods.py
  - docs/api-reference.md
---

# ACL & Multi-tenant Semantics (Client Contract)

Issue #141, item 3. This page is the contract `@fusion/artifacts-client` consumers
(fusion-office#66 and later) must follow; the adversarial examples below are what
the consumer ACL tests assert against. Engine source of truth:
`RPCHandler._enforce_owner` in `fusion_artifacts_engine/rpc/methods.py`.

## Fields the client must send

| Field | Direction | Meaning |
| --- | --- | --- |
| `project_id` | create / list / get / update / delete | Scope selector. Set on create to stamp ownership scope; passed on reads/writes to constrain lookup. |
| `owner_user_id` | create only | Who owns the artifact (multi-tenant). `None` = single-tenant default. |
| `ownership_type` | create only | Ownership model tag (default `free`). |
| `caller_user_id` | any op | Identity of the acting user. **Omit = no ownership check** (single-tenant mode). Multi-tenant deployments MUST inject it per request. |
| `X-API-Key` header | transport | Engine auth (C-4 precedence: `FUSION_ARTIFACTS_API_KEY` env first, then `security.api_key` config). Missing/wrong key = `Unauthorized` (-32600) before any ACL logic. |

## Enforcement semantics

- The IDOR guard is active **only when both** `caller_user_id` (request) and
  `owner_user_id` (artifact) are present. Either side `None` → check skipped
  (single-tenant backward compatibility, no false positives).
- Mismatch → `PermissionError` mapped to JSON-RPC `-32006` (permission/IDOR).
  Clients must treat `-32006` as authorization failure, never retry.
- Cross-tenant isolation relies on the caller sending the correct
  `caller_user_id` + `project_id`; the engine does not derive tenancy from the
  API key. A gateway in front of the engine (fusion-identity tenant tokens →
  `caller_user_id`) is the trust boundary.

## Adversarial examples (assert these in consumer tests)

| Attempt | Expected |
| --- | --- |
| Read artifact A (owner `u1`) with `caller_user_id: "u2"` | `-32006` denied |
| Update artifact A (owner `u1`) with `caller_user_id: "u2"` | `-32006` denied |
| Same caller with `caller_user_id: "u1"` | success |
| Omit `caller_user_id` entirely (single-tenant client) | success (check skipped — document this in your threat model) |
| Wrong `X-API-Key` | `-32600 Unauthorized` (auth fails before ACL) |
| Create with `owner_user_id: "u1"`, later act as `caller_user_id: "u2"` | `-32006` denied |

## Retry safety (offline reconciliation)

Issue #141, item 4. Engine-side idempotency lives in `artifact.create`:
send `idempotency_key` (stored in artifact metadata as `_idempotency_key`); a
replayed create within the same `session_id`/`project_id` returns the original
artifact with `deduplicated: true` instead of duplicating.

| Method class | Retry-safe? | Mechanism |
| --- | --- | --- |
| Read-only (`artifact.get`, `get_content`, `list`, `load`, `toc`, `view`, `version_list`, `version_diff`, `list_snapshots`, `get_shared`, `search_library`, `list_events`, `list_comments`, `list_recycle`, `ping`, `ai.list_models`) | always | no state change |
| `artifact.create` | when `idempotency_key` sent | metadata key dedupe |
| `artifact.update` | when `expected_content_hash` sent | optimistic locking → `-32002` conflict instead of lost update |
| `artifact.patch` | with `expected_version` | version guard |
| everything else mutating (`rename`, `star`, `pin`, `duplicate`, `remix`, `delete`, `restore`, share/snapshot create, folders/tags, workflow.*) | check-then-act | client must read current state before retrying |

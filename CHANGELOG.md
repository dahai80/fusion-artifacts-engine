---
doc_id: fusion-artifacts-engine/changelog
product: fusion-artifacts-engine
class: app
entry: service
doc_type: changelog
title: { zh: "变更日志", en: "Changelog" }
audience: engineer
status: draft
version: 0.7.17
updated: 2026-11-25
owner: "@dahai"
source_of_truth:
  - CHANGELOG.md
---

# Changelog

All notable changes to fusion-artifacts-engine are documented here.

## 0.7.17 - 2026-11-25

### Added

- Issue #128: pptx write-back. `markdown_to_pptx` (in-process python-pptx, same pattern as docx/openpyxl) regenerates a deck from patched markdown — headings become slide titles, paragraphs fill the body placeholder, markdown tables render as pptx tables. `maybe_write_back` now dispatches pptx to `.outbox/{artifact_id}_v{num}.pptx`; the user's original file is never touched. Note: fusion-office office-api has no `/v1/pptx/write` endpoint (upstream issue filed), so generation is in-process.
- Issue #129: first-class library search + archive retry. New RPC `artifact.search_library` (query, top_k, threshold, hybrid, filters) queries fusion-rag synchronously over archived versions and merges hits with local metadata rows (`source: rag|metadata`, ranked by score); rag outage degrades to metadata-only with `rag_status: unavailable|disabled|ok`. Archive failures are no longer log-only: recorded in `metadata.rag_archive` (`{failed, last_error, status, updated_at}`) and retryable via new RPC `artifact.retry_archive` (returns `retried`/`failed`/`status`).

## 0.7.16 - 2026-11-16

### Changed

- CI retrofit per fusion#2 checklist (issue #126): `concurrency` groups with `cancel-in-progress` on ci.yml/docs.yml, serialized (non-cancel) release.yml; pip cache on `setup-python` keyed on `pyproject.toml`. Sharding/tiered gate assessed as not warranted (single matrix job, ~3 min suite).

## 0.7.15 - 2026-11-13

### Fixed

- Issue #124: `render.visual` dsl-object path now schema-validates (`validate_dsl_object` in `dsl_parser.py`, fusion_core first, in-repo fallback when absent) — an incomplete DSL dict reports `valid: false` with a non-empty `errors` list instead of silently rendering with `valid: true`. Render always still returns HTML via the 3-level fallback chain.
- start.sh health probe: bare system `python3` without PyYAML could not read `security.api_key` from config and pinged unauthenticated (401), misreporting a healthy daemon as `not running`. Regex fallback added for the key read.

## 0.7.14 - 2026-11-07

### Docs

- Adopt the fusion docs-system v1.1 contract (app x service): added `docs/product-page.md`, `docs/get-started.md`, `docs/api-reference.md`, `docs/config-reference.md`, `docs/troubleshooting.md`; added 12-key frontmatter to all in-scope markdown files; added `ci/validate_docs.py` and `.github/workflows/docs.yml`.
- Corrected docs to match the live RPC surface: `artifact.create` requires `name` (not `title`); `/metrics` auth header is `X-Metrics-Token` (not `X-API-Token`).

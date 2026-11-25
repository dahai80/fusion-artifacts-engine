---
doc_id: fusion-artifacts-engine/changelog
product: fusion-artifacts-engine
class: app
entry: service
doc_type: changelog
title: { zh: "变更日志", en: "Changelog" }
audience: engineer
status: draft
version: 0.7.18
updated: 2026-11-25
owner: "@dahai"
source_of_truth:
  - CHANGELOG.md
---

# Changelog

All notable changes to fusion-artifacts-engine are documented here.

## 0.7.18 - 2026-11-25

### Added

- PyPI + Homebrew distribution. `.github/workflows/pypi.yml` publishes sdist+wheel to PyPI via Trusted Publishing (OIDC) on `v*` tags (one-time trusted-publisher registration on pypi.org required); `readme` metadata added to pyproject so the PyPI page renders README.md. `Formula/fusion-artifacts-engine.rb` (repo doubles as a tap: `brew install dahai80/fusion-artifacts-engine/fusion-artifacts-engine`) builds from a release tag into an isolated Homebrew venv; `HOMEBREW_PIP_INDEX_URL` passthrough for mirror networks. Verified end-to-end locally: fresh-venv wheel install + CLI smoke, `brew install` + `brew list/info`.

### Fixed

- Issue #131: pptx write-back lost slide titles — `_parse_pptx` flattened the title into body lines, so `markdown_to_pptx` regenerated decks with an empty title placeholder. Titled slides now emit the title as the section heading (`## <title>`; untitled slides keep the stable `## Slide N` anchor) and the title shape is skipped in body extraction (shape_id compare — `shapes.title` returns a fresh wrapper per access, identity comparison never matches). Regression test: real pptx → parse_office → markdown_to_pptx → title placeholder equals source title. docx/xlsx roundtrip fidelity unchanged.
- `fusion-artifacts-engine status` misreported a healthy daemon as "Not running" when `FUSION_ARTIFACTS_API_KEY` was unset: the probe pinged unauthenticated instead of falling back to `config.api_key` (same C-4 precedence as rpc/server.py, same bug class as the start.sh probe fixed in 0.7.15). `test_status_not_running` now isolates from any real daemon/config on the machine.

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

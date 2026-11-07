---
doc_id: fusion-artifacts-engine/changelog
product: fusion-artifacts-engine
class: app
entry: service
doc_type: changelog
title: { zh: "变更日志", en: "Changelog" }
audience: engineer
status: draft
version: 0.7.14
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - CHANGELOG.md
---

# Changelog

All notable changes to fusion-artifacts-engine are documented here.

## 0.7.14 - 2026-11-07

### Docs

- Adopt the fusion docs-system v1.1 contract (app x service): added `docs/product-page.md`, `docs/get-started.md`, `docs/api-reference.md`, `docs/config-reference.md`, `docs/troubleshooting.md`; added 12-key frontmatter to all in-scope markdown files; added `ci/validate_docs.py` and `.github/workflows/docs.yml`.
- Corrected docs to match the live RPC surface: `artifact.create` requires `name` (not `title`); `/metrics` auth header is `X-Metrics-Token` (not `X-API-Token`).

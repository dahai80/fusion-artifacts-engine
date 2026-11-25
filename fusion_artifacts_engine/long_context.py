import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

# v0.7.1 #91: layered context compaction. compactor remains the fallback; this module provides the two primary mechanisms:
# 1) Rolling summary — once the version count passes the threshold, fusion-mlx generates a changelog summary written back to
#    artifact.summary (ref_text picks it up automatically via the existing pipeline) + metadata.summary_version for dedup.
# 2) RAG archival — when a version is superseded, push the old version into fusion-rag
#    (POST /bases/{kb}/documents/ingest, doc_name=art_x_vN, metadata carrying
#    artifact_id/version/type); metadata.archived_versions is the idempotent dedup guard.
# Both paths only log on failure and never block version creation/patch results.
# v0.7.17 #129: archive failures are no longer silent — they are recorded in
# metadata.rag_archive ({failed: [vnum...], last_error, status, updated_at}) and
# retryable via artifact.retry_archive; first-class library search (artifact.search_library)
# queries fusion-rag synchronously and merges with local metadata-filter results.

_ARCHIVE_TAG = "archived_versions"
_ARCHIVE_STATUS_TAG = "rag_archive"


def _mlx_config(engine) -> tuple[str, str, str]:
    cfg = engine.config
    api_key = cfg.long_context_mlx_api_key or os.environ.get("FUSION_MLX_API_KEY", "")
    return cfg.long_context_mlx_url, api_key, cfg.long_context_summary_model


def _build_changelog_digest(engine, artifact_id: str, limit: int = 40) -> str:
    versions = engine.storage.list_versions(
        artifact_id, page=1, page_size=limit, include_content=False
    )
    lines = []
    for v in reversed(versions):
        change = (v.change_log or "(no change log)").strip().replace("\n", " ")
        lines.append(f"v{v.version_num}: {change}")
    return "\n".join(lines)


async def maybe_generate_summary(engine, artifact, version_num: int) -> str | None:
    # When the version count reaches the threshold and no summary exists yet for this version, call fusion-mlx to generate the rolling changelog.
    cfg = engine.config
    threshold = cfg.long_context_summary_threshold
    if threshold <= 0:
        return None
    # Background tasks may run after other metadata writes — re-read the latest row, never trust the caller's snapshot
    fresh = engine.storage.get_artifact(artifact.id)
    if fresh is not None:
        artifact = fresh
    meta = artifact.metadata or {}
    if meta.get("summary_version") == version_num:
        return None
    try:
        count = len(
            engine.storage.list_versions(artifact.id, page=1, page_size=500, include_content=False)
        )
        if count < threshold:
            return None
        digest = _build_changelog_digest(engine, artifact.id)
        base_url, api_key, model = _mlx_config(engine)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {
            "model": model or "default",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Summarize the evolution of this artifact in <=120 words. "
                        "State what it is now and the key changes across versions. "
                        "Output the summary text only."
                    ),
                },
                {"role": "user", "content": digest},
            ],
            "max_tokens": 300,
            "temperature": 0.2,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            summary = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        if not summary:
            logger.warning("long-context: empty summary from mlx for %s", artifact.id)
            return None
        engine.storage.update_artifact_summary(
            artifact.id,
            summary,
            metadata_patch={"summary_version": version_num},
        )
        logger.info(
            "long-context: rolling summary generated %s v%d (%d chars)",
            artifact.id,
            version_num,
            len(summary),
        )
        return summary
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "long-context: summary generation failed %s v%d (degrade, non-fatal): %s",
            artifact.id,
            version_num,
            e,
        )
        return None


async def maybe_archive_superseded(engine, artifact, superseded_version_num: int) -> str | None:
    # After a new version supersedes the old one, archive the old version's content into fusion-rag. Idempotent: each version is archived once.
    cfg = engine.config
    if not cfg.long_context_rag_enabled:
        return None
    # Background tasks may run after other metadata writes — re-read the latest row, never trust the caller's snapshot
    fresh = engine.storage.get_artifact(artifact.id)
    if fresh is not None:
        artifact = fresh
    meta = artifact.metadata or {}
    archived = meta.get(_ARCHIVE_TAG) or []
    if superseded_version_num in archived:
        return None
    if superseded_version_num < 1:
        return None
    ok, err = await _archive_version(engine, artifact, superseded_version_num)
    _record_archive_result(engine, artifact.id, superseded_version_num, err if not ok else None)
    if ok:
        return f"{artifact.id}_v{superseded_version_num}"
    return None


async def _archive_version(engine, artifact, version_num: int) -> tuple[bool, str | None]:
    # Core archive push for one version. Returns (ok, error). Never raises.
    cfg = engine.config
    try:
        version = engine.storage.get_version(artifact.id, version_num)
        if version is None:
            logger.warning(
                "long-context: archive skip, version %s v%d not found",
                artifact.id,
                version_num,
            )
            return False, f"version {version_num} not found"
        base_url = cfg.long_context_rag_url.rstrip("/")
        api_key = cfg.long_context_rag_api_key or os.environ.get("FUSION_RAG_API_KEY", "")
        headers = {"X-API-Key": api_key} if api_key else {}
        body = {
            "content": version.content,
            "content_type": "text",
            "doc_name": f"{artifact.id}_v{version_num}.txt",
            "metadata": {
                "artifact_id": artifact.id,
                "artifact_version": version_num,
                "artifact_type": artifact.type,
                "name": artifact.name,
                "archived_at": version.created_at,
            },
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/bases/{cfg.long_context_rag_kb_id}/documents/ingest",
                json=body,
                headers=headers,
            )
            if resp.status_code >= 400:
                detail = resp.text[:200]
                logger.warning(
                    "long-context: rag archive rejected %s v%d: HTTP %d %s",
                    artifact.id,
                    version_num,
                    resp.status_code,
                    detail,
                )
                return False, f"HTTP {resp.status_code}: {detail}"
        prev_archived = (artifact.metadata or {}).get(_ARCHIVE_TAG) or []
        new_archived = sorted({*prev_archived, version_num})
        engine.storage.update_artifact_summary(
            artifact.id,
            artifact.summary or "",
            metadata_patch={_ARCHIVE_TAG: new_archived},
        )
        logger.info(
            "long-context: archived %s v%d into rag kb=%s",
            artifact.id,
            version_num,
            cfg.long_context_rag_kb_id,
        )
        return True, None
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "long-context: rag archive failed %s v%d (degrade, non-fatal): %s",
            artifact.id,
            version_num,
            e,
        )
        return False, str(e)


def _record_archive_result(engine, artifact_id: str, version_num: int, error: str | None) -> None:
    # Issue #129: surface archive state in metadata.rag_archive — failures are
    # visible and retryable via artifact.retry_archive instead of log-only.
    try:
        fresh = engine.storage.get_artifact(artifact_id)
        if fresh is None:
            return
        meta = fresh.metadata or {}
        status = dict(meta.get(_ARCHIVE_STATUS_TAG) or {})
        failed = set(status.get("failed") or [])
        if error is None:
            failed.discard(version_num)
            if failed:
                status["status"] = "failed"
            else:
                status["status"] = "ok"
                status.pop("last_error", None)
        else:
            failed.add(version_num)
            status["status"] = "failed"
            status["last_error"] = str(error)[:300]
        status["failed"] = sorted(failed)
        status["updated_at"] = time.time()
        engine.storage.update_artifact_summary(
            artifact_id,
            fresh.summary or "",
            metadata_patch={_ARCHIVE_STATUS_TAG: status},
        )
    except Exception:  # noqa: BLE001
        logger.exception("long-context: failed to record archive state for %s", artifact_id)


async def retry_failed_archives(engine, artifact_id: str) -> dict:
    # Issue #129 acceptance: archive failure visible + retryable from the API.
    cfg = engine.config
    if not cfg.long_context_rag_enabled:
        return {"retried": [], "failed": [], "status": "disabled"}
    fresh = engine.storage.get_artifact(artifact_id)
    if fresh is None:
        raise ValueError(f"artifact {artifact_id} not found")
    meta = fresh.metadata or {}
    status = meta.get(_ARCHIVE_STATUS_TAG) or {}
    failed = status.get("failed") or []
    archived = meta.get(_ARCHIVE_TAG) or []
    todo = [v for v in failed if v not in archived]
    retried: list[int] = []
    still_failed: list[int] = []
    for vnum in todo:
        ok, err = await _archive_version(engine, fresh, vnum)
        _record_archive_result(engine, artifact_id, vnum, None if ok else err)
        (retried if ok else still_failed).append(vnum)
    final = engine.storage.get_artifact(artifact_id)
    final_status = ((final.metadata or {}).get(_ARCHIVE_STATUS_TAG) or {}) if final else {}
    logger.info(
        "long-context: retry archive %s -> retried=%s failed=%s",
        artifact_id,
        retried,
        still_failed,
    )
    return {
        "retried": retried,
        "failed": still_failed,
        "status": final_status.get("status", "ok"),
        "last_error": final_status.get("last_error"),
    }


def _search_row(
    artifact, version_num: int | None, source: str, score: float | None, snippet: str | None
) -> dict:
    row = {
        "artifact_id": artifact.id,
        "name": artifact.name,
        "type": artifact.type,
        "kind": artifact.kind,
        "version": version_num if version_num is not None else artifact.current_version,
        "source": source,
        "updated_at": artifact.updated_at,
    }
    if score is not None:
        row["score"] = round(float(score), 6)
    if snippet:
        row["snippet"] = snippet[:200]
    return row


async def search_library(
    engine,
    query: str,
    top_k: int = 10,
    threshold: float = 0.0,
    hybrid: bool = False,
    filters: dict | None = None,
) -> dict:
    # Issue #129: first-class library search. Queries fusion-rag synchronously over
    # archived artifact content (kb=long_context_rag_kb_id) and merges with local
    # metadata-filter results. RAG unavailability degrades to metadata-only results
    # with rag_status="unavailable" — never fails the whole search.
    if not query or not str(query).strip():
        raise ValueError("query is required")
    cfg = engine.config
    top_k = max(int(top_k), 1)
    local_artifacts, _total = engine.list_all_artifacts(
        filters=filters, sort="updated_at", page=1, page_size=top_k
    )
    results: dict[str, dict] = {}
    for a in local_artifacts:
        results[a.id] = _search_row(a, None, "metadata", None, None)
    if not cfg.long_context_rag_enabled:
        return {"results": list(results.values()), "rag_status": "disabled"}
    base_url = cfg.long_context_rag_url.rstrip("/")
    api_key = cfg.long_context_rag_api_key or os.environ.get("FUSION_RAG_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else {}
    body = {
        "query": str(query),
        "top_k": top_k,
        "threshold": float(threshold),
        "hybrid": bool(hybrid),
    }
    rag_status = "ok"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url}/bases/{cfg.long_context_rag_kb_id}/search",
                json=body,
                headers=headers,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "long-context: library search rejected: HTTP %d %s",
                    resp.status_code,
                    resp.text[:200],
                )
                rag_status = "unavailable"
            else:
                hits = resp.json() or []
        if rag_status == "ok":
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                meta = hit.get("metadata") or {}
                if isinstance(meta, str):
                    import json as _json

                    try:
                        meta = _json.loads(meta)
                    except Exception:  # noqa: BLE001
                        meta = {}
                aid = meta.get("artifact_id")
                if not aid:
                    continue
                vnum = meta.get("artifact_version")
                snippet = hit.get("content") or hit.get("text") or ""
                row = _search_row_of(hit, aid, vnum, snippet)
                existing = results.get(aid)
                if existing is None or "score" not in existing:
                    results[aid] = row
    except Exception as e:  # noqa: BLE001
        logger.warning("long-context: library search rag unavailable (degrade): %s", e)
        rag_status = "unavailable"
    ranked = sorted(
        results.values(),
        key=lambda r: (-r.get("score", 0.0), -(r.get("updated_at") or 0)),
    )
    return {"results": ranked, "rag_status": rag_status}


def _search_row_of(hit: dict, artifact_id: str, version_num, snippet: str) -> dict:
    meta = hit.get("metadata") or {}
    return {
        "artifact_id": artifact_id,
        "name": meta.get("name") or artifact_id,
        "type": meta.get("artifact_type"),
        "kind": None,
        "version": version_num,
        "source": "rag",
        "score": round(float(hit.get("score") or 0.0), 6),
        "snippet": snippet[:200],
        "updated_at": meta.get("archived_at"),
    }


def history_query_hint(engine, artifact) -> dict:
    # #91 acceptance: the RAG query pattern handed to consumers (no new RPC surface — documenting the usage is enough).
    cfg = engine.config
    return {
        "rag_url": cfg.long_context_rag_url,
        "kb_id": cfg.long_context_rag_kb_id,
        "filter": {"artifact_id": artifact.id},
        "note": "query fusion-rag /bases/{kb_id}/search with filter.artifact_id; doc_name=<artifact_id>_v<N>.txt",
    }

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# v0.7.1 #91: 分层上下文压缩。compactor 保持兜底；本模块提供两层主机制：
# 1) 滚动摘要——版本数过阈值后用 fusion-mlx 生成 changelog 摘要写回
#    artifact.summary（ref_text 经既有管道自动带上）+ metadata.summary_version 防重。
# 2) RAG 归档——版本被取代时把旧版本推入 fusion-rag
#    （POST /bases/{kb}/documents/ingest，doc_name=art_x_vN，metadata 带
#    artifact_id/version/type），metadata.archived_versions 幂等防重。
# 两条链路失败只记日志，绝不阻断版本创建/patch 结果。

_ARCHIVE_TAG = "archived_versions"


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
    # 版本数达阈值且该版本尚未生成过摘要时，调 fusion-mlx 生成滚动 changelog。
    cfg = engine.config
    threshold = cfg.long_context_summary_threshold
    if threshold <= 0:
        return None
    # 后台任务可能晚于其它元数据写入执行——重读最新行，勿信调用方传入的快照
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
    # 新版本取代旧版本后，把旧版本内容归档进 fusion-rag。幂等：同一版本只归档一次。
    cfg = engine.config
    if not cfg.long_context_rag_enabled:
        return None
    # 后台任务可能晚于其它元数据写入执行——重读最新行，勿信调用方传入的快照
    fresh = engine.storage.get_artifact(artifact.id)
    if fresh is not None:
        artifact = fresh
    meta = artifact.metadata or {}
    archived = meta.get(_ARCHIVE_TAG) or []
    if superseded_version_num in archived:
        return None
    if superseded_version_num < 1:
        return None
    try:
        version = engine.storage.get_version(artifact.id, superseded_version_num)
        if version is None:
            logger.warning(
                "long-context: archive skip, version %s v%d not found",
                artifact.id,
                superseded_version_num,
            )
            return None
        base_url = cfg.long_context_rag_url.rstrip("/")
        api_key = cfg.long_context_rag_api_key or os.environ.get("FUSION_RAG_API_KEY", "")
        headers = {"X-API-Key": api_key} if api_key else {}
        body = {
            "content": version.content,
            "content_type": "text",
            "doc_name": f"{artifact.id}_v{superseded_version_num}.txt",
            "metadata": {
                "artifact_id": artifact.id,
                "artifact_version": superseded_version_num,
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
                logger.warning(
                    "long-context: rag archive rejected %s v%d: HTTP %d %s",
                    artifact.id,
                    superseded_version_num,
                    resp.status_code,
                    resp.text[:200],
                )
                return None
        new_archived = sorted({*archived, superseded_version_num})
        engine.storage.update_artifact_summary(
            artifact.id,
            artifact.summary or "",
            metadata_patch={_ARCHIVE_TAG: new_archived},
        )
        logger.info(
            "long-context: archived %s v%d into rag kb=%s",
            artifact.id,
            superseded_version_num,
            cfg.long_context_rag_kb_id,
        )
        return f"{artifact.id}_v{superseded_version_num}"
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "long-context: rag archive failed %s v%d (degrade, non-fatal): %s",
            artifact.id,
            superseded_version_num,
            e,
        )
        return None


def history_query_hint(engine, artifact) -> dict:
    # #91 验收：给消费者的 rag 查询模式（不做新 RPC 面proxy——文档化用法即可）。
    cfg = engine.config
    return {
        "rag_url": cfg.long_context_rag_url,
        "kb_id": cfg.long_context_rag_kb_id,
        "filter": {"artifact_id": artifact.id},
        "note": "query fusion-rag /bases/{kb_id}/search with filter.artifact_id; doc_name=<artifact_id>_v<N>.txt",
    }

import logging

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage
from fusion_artifacts_engine.token_counter import count_tokens

logger = logging.getLogger(__name__)


# H7: token budget logic extracted from engine.py. Pure functions taking config+storage, behavior unchanged.


def context_budget(
    config: ArtifactEngineConfig,
    storage: SQLiteStorage,
    session_id: str | None = None,
    context_window: int | None = None,
) -> dict:
    # P-2: aggregate tokens with a single storage-layer SQL instead of N+1 per-version reads + an unbounded page_size=100000.
    if context_window is not None and context_window < 1:
        raise ValueError("context_window must be >= 1")
    total_tokens, artifact_list = storage.sum_token_counts(session_id=session_id)
    effective_window = (
        context_window
        if context_window is not None
        else config.context_budget_default
    )
    utilization_pct = (
        round(total_tokens / effective_window * 100, 1)
        if effective_window > 0
        else 0.0
    )
    warning = utilization_pct > 70
    recommendation = None
    if warning:
        recommendation = "Consider using preview_only mode for artifact injection."
    logger.info(
        "Context budget session=%s total_tokens=%d window=%d utilization=%.1f%% warning=%s",
        session_id,
        total_tokens,
        effective_window,
        utilization_pct,
        warning,
    )
    return {
        "total_artifact_tokens": total_tokens,
        "artifact_count": len(artifact_list),
        "artifacts": artifact_list,
        "context_window": effective_window,
        "utilization_percent": utilization_pct,
        "warning": warning,
        "recommendation": recommendation,
    }


def check_safety(
    config: ArtifactEngineConfig,
    messages: list[dict],
    output_budget: int | None = None,
) -> dict:
    current_tokens = 0
    for msg in messages:
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        current_tokens += count_tokens(content)
    effective_budget = (
        output_budget
        if output_budget and output_budget > 0
        else config.context_budget_default
    )
    remaining = effective_budget - current_tokens
    safe = remaining >= 0
    logger.info(
        "check_safety: current=%d budget=%d remaining=%d safe=%s",
        current_tokens,
        effective_budget,
        remaining,
        safe,
    )
    return {
        "safe": safe,
        "current_tokens": current_tokens,
        "remaining_tokens": remaining,
    }


def inject(
    config: ArtifactEngineConfig,
    messages: list[dict],
    output_budget: int | None = None,
) -> dict:
    # R7/STUB: inject is a placeholder implementation — only a token budget check; it injects nothing and does not modify messages.
    # Not a future extension — a capability that was advertised but never implemented. Commercial integrations must not
    # rely on injection side effects.
    # README/SDK already flag it as a stub (see Batch8 doc corrections). Returns injected=False + stub=True.
    total_tokens = 0
    for msg in messages:
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        total_tokens += count_tokens(content)
    effective_budget = (
        output_budget
        if output_budget and output_budget > 0
        else config.context_budget_default
    )
    safe = total_tokens <= effective_budget
    logger.info(
        "inject: messages=%d total_tokens=%d budget=%d safe=%s",
        len(messages),
        total_tokens,
        effective_budget,
        safe,
    )
    return {
        "messages": messages,
        "total_tokens": total_tokens,
        "safe": safe,
        "injected": False,
        "stub": True,
        "note": "STUB: budget check only, messages unchanged; inject not implemented",
    }

import logging
from fusion_artifacts_engine.models import ArtifactRef
from fusion_artifacts_engine.ref_parser import parse_refs_from_message, generate_ref_text

logger = logging.getLogger(__name__)


def _wrap_content(content: str, artifact_type: str) -> str:
    if artifact_type in ("html", "react"):
        return f"\n<html-artifact>\n{content}\n</html-artifact>\n"
    if artifact_type == "markdown":
        return f"\n{content}\n"
    return f"\n```\n{content}\n```\n"


async def inject_artifacts_to_messages(
    messages: list[dict],
    get_content_fn,
    token_counter,
    max_context: int = 180_000,
    reserve_output: int = 8192,
) -> tuple[list[dict], int, bool]:
    injected = []
    for msg in messages:
        injected.append(dict(msg))

    msg_refs: list[tuple[int, list[ArtifactRef]]] = []
    for i, msg in enumerate(injected):
        content = msg.get("content", "")
        if isinstance(content, str):
            refs = parse_refs_from_message(content)
            if refs:
                msg_refs.append((i, refs))
        elif isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "text":
                    refs = parse_refs_from_message(block.get("text", ""))
                    if refs:
                        msg_refs.append((i, refs))

    all_refs = [r for _, refs in msg_refs for r in refs]

    if not all_refs:
        total = await token_counter.count_messages(injected)
        safe = (max_context - total - reserve_output) >= 0
        return injected, total, safe

    for msg_idx, refs in msg_refs:
        msg = injected[msg_idx]
        content = msg.get("content", "")

        if isinstance(content, str):
            new_text = content
            for ref in refs:
                try:
                    version_content = await get_content_fn(ref.artifact_id, ref.version)
                    if version_content is None:
                        logger.warning("Inject: content not found for %s v%s", ref.artifact_id, ref.version)
                        continue
                    wrapped = _wrap_content(version_content, ref.type)
                    ref_text = generate_ref_text(
                        ref.artifact_id, ref.name, ref.type,
                        int(ref.version) if ref.version != "latest" else 0,
                        ref.token_count, ref.summary,
                    )
                    new_text = new_text.replace(ref_text, wrapped, 1)
                except Exception as e:
                    logger.error("Inject error for %s: %s", ref.artifact_id, e)
            msg["content"] = new_text

        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if not text:
                        continue
                    block_refs = parse_refs_from_message(text)
                    if not block_refs:
                        continue
                    new_text = text
                    for ref in block_refs:
                        try:
                            version_content = await get_content_fn(ref.artifact_id, ref.version)
                            if version_content is None:
                                logger.warning("Inject: content not found for %s v%s", ref.artifact_id, ref.version)
                                continue
                            wrapped = _wrap_content(version_content, ref.type)
                            ref_text = generate_ref_text(
                                ref.artifact_id, ref.name, ref.type,
                                int(ref.version) if ref.version != "latest" else 0,
                                ref.token_count, ref.summary,
                            )
                            new_text = new_text.replace(ref_text, wrapped, 1)
                        except Exception as e:
                            logger.error("Inject error for %s: %s", ref.artifact_id, e)
                    block["text"] = new_text

    total = await token_counter.count_messages(injected)
    safe = (max_context - total - reserve_output) >= 0
    logger.info("Injected %s refs, total_tokens=%s, safe=%s", len(all_refs), total, safe)
    return injected, total, safe

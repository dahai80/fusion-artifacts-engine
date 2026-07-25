import logging
from typing import Optional
from fusion_artifacts_engine.models import ArtifactRef
from fusion_artifacts_engine.ref_parser import parse_refs_from_message, generate_ref_text

logger = logging.getLogger(__name__)


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

    all_refs = []
    for msg in injected:
        content = msg.get("content", "")
        if isinstance(content, str):
            all_refs.extend(parse_refs_from_message(content))
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    all_refs.extend(parse_refs_from_message(block.get("text", "")))

    if not all_refs:
        total = await token_counter.count_messages(injected)
        safe = total + reserve_output <= max_context
        return injected, total, safe

    for msg in injected:
        content = msg.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    break

        if not text:
            continue

        refs = parse_refs_from_message(text)
        if not refs:
            continue

        new_text = text
        for ref in refs:
            try:
                version_content = await get_content_fn(ref.artifact_id, ref.version)
                if version_content is None:
                    logger.warning(f"Inject: content not found for {ref.artifact_id} v{ref.version}")
                    continue
                ref_text = generate_ref_text(
                    ref.artifact_id, ref.name, ref.type,
                    int(ref.version) if ref.version != "latest" else 0,
                    ref.token_count, ref.summary,
                )
                code_block = f"\n```\n{version_content}\n```\n"
                new_text = new_text.replace(ref_text, code_block)
            except Exception as e:
                logger.error(f"Inject error for {ref.artifact_id}: {e}")

        if isinstance(content, str):
            msg["content"] = new_text
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    block["text"] = new_text
                    break

    total = await token_counter.count_messages(injected)
    safe = total + reserve_output <= max_context
    logger.info(f"Injected {len(all_refs)} refs, total_tokens={total}, safe={safe}")
    return injected, total, safe

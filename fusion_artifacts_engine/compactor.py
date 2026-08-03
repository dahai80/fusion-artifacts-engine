import logging
import re

from fusion_artifacts_engine.token_counter import count_tokens

logger = logging.getLogger(__name__)


def compact_content(content: str, artifact_type: str, token_budget: int) -> str:
    original_tokens = count_tokens(content)
    if original_tokens <= token_budget:
        logger.debug(
            "Content already within budget: %d <= %d", original_tokens, token_budget
        )
        return content

    result = content
    result = _remove_comments(result, artifact_type)
    if count_tokens(result) <= token_budget:
        return result

    result = _collapse_blank_lines(result)
    if count_tokens(result) <= token_budget:
        return result

    result = _remove_decorators(result, artifact_type)
    if count_tokens(result) <= token_budget:
        return result

    result = _truncate_sections(result, artifact_type, token_budget)
    return result


def _remove_comments(content: str, artifact_type: str) -> str:
    lines = content.split("\n")
    out = []
    if artifact_type == "code":
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            out.append(line)
    elif artifact_type == "markdown":
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("<!--") and "-->" in stripped:
                continue
            out.append(line)
    else:
        return content
    logger.debug(
        "Removed comments: %d -> %d lines for %s", len(lines), len(out), artifact_type
    )
    return "\n".join(out)


def _collapse_blank_lines(content: str) -> str:
    result = re.sub(r"\n{3,}", "\n\n", content)
    result = result.strip() + "\n"
    return result


def _remove_decorators(content: str, artifact_type: str) -> str:
    if artifact_type != "markdown":
        return content
    lines = content.split("\n")
    out = [
        line
        for line in lines
        if line.strip() not in ("---", "***", "___", "- - -", "* * *")
    ]
    logger.debug("Removed decorators: %d -> %d lines", len(lines), len(out))
    return "\n".join(out)


def _truncate_sections(content: str, artifact_type: str, token_budget: int) -> str:
    lines = content.split("\n")
    sections = []
    i = 0
    while i < len(lines):
        matched = False
        level = 0
        anchor = ""
        if artifact_type == "markdown":
            m = re.match(r"^(#{1,6})\s+(.+)$", lines[i])
            if m:
                anchor = m.group(2).strip()
                level = len(m.group(1))
                matched = True
        elif artifact_type == "code":
            m = re.match(r"^(?:async\s+)?(?:def|class|func)\s+(\w+)", lines[i])
            if m:
                anchor = m.group(1)
                level = 1
                matched = True
        if matched:
            sections.append({"start": i, "anchor": anchor, "level": level})
        i += 1

    if not sections:
        truncated = "\n".join(lines)
        if count_tokens(truncated) <= token_budget:
            return truncated
        ratio = token_budget / max(1, count_tokens(truncated))
        cut = int(len(lines) * ratio)
        return "\n".join(lines[:cut])

    sections.reverse()
    for sec in sections:
        current = "\n".join(lines[: sec["start"]])
        if count_tokens(current) <= token_budget:
            logger.info(
                "Truncated sections from anchor='%s' to meet budget=%d",
                sec["anchor"],
                token_budget,
            )
            return current
    return "\n".join(lines[:1]) if lines else ""

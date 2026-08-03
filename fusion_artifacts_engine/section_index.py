import logging
import re

logger = logging.getLogger(__name__)


def normalize_anchor(anchor: str) -> str:
    normalized = anchor.strip().lstrip("#").strip()
    logger.debug("normalize_anchor: '%s' -> '%s'", anchor, normalized)
    return normalized


def extract_sections(content: str, artifact_type: str) -> list[dict]:
    sections = []
    if artifact_type == "markdown":
        for m in re.finditer(r"^(#{1,6})\s+(.+)$", content, re.MULTILINE):
            sections.append({"anchor": m.group(2).strip(), "level": len(m.group(1))})
    elif artifact_type == "code":
        for m in re.finditer(
            r"^(?:async\s+)?(?:def|class|func)\s+(\w+)", content, re.MULTILINE
        ):
            sections.append({"anchor": m.group(1), "level": 1})
    return sections

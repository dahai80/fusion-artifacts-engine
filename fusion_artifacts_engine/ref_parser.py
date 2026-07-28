import re
import logging
from fusion_artifacts_engine.models import ArtifactRef

logger = logging.getLogger(__name__)

_TOOL_RESULT_PATTERN = re.compile(
    r"\[Artifact:\s*(?P<name>[^|]+)\s*\|\s*ID:\s*(?P<id>\w+)\s*\|\s*Version:\s*v(?P<version>\d+)\s*\|\s*Type:\s*(?P<type>\w+)\s*\|\s*Tokens:\s*(?P<tokens>\d+)\s*(?:\|\s*Summary:\s*(?P<summary>.+?))?\]",
    re.IGNORECASE,
)

_XML_ARTIFACT_PATTERN = re.compile(
    r"<artifact\s+id=[\"'](?P<id>[^\"']+)[\"']\s+name=[\"'](?P<name>[^\"']+)[\"']\s+type=[\"'](?P<type>[^\"']+)[\"']\s+version=[\"'](?P<version>[^\"']+)[\"']\s+token_count=[\"'](?P<tokens>\d+)[\"']\s*>\s*(?:<summary>(?P<summary>[^<]*)</summary>)?\s*</artifact>",
    re.DOTALL,
)


def parse_refs_from_message(content: str) -> list[ArtifactRef]:
    refs = []
    for m in _TOOL_RESULT_PATTERN.finditer(content):
        ver = m.group("version")
        try:
            ver = str(int(ver))
        except ValueError:
            pass
        refs.append(ArtifactRef(
            artifact_id=m.group("id"),
            name=m.group("name").strip(),
            type=m.group("type").strip(),
            version=ver,
            token_count=int(m.group("tokens")),
            summary=(m.group("summary") or "").strip(),
        ))
    for m in _XML_ARTIFACT_PATTERN.finditer(content):
        ver = m.group("version")
        if ver != "latest":
            try:
                ver = str(int(ver))
            except ValueError:
                pass
        refs.append(ArtifactRef(
            artifact_id=m.group("id"),
            name=m.group("name").strip(),
            type=m.group("type").strip(),
            version=ver,
            token_count=int(m.group("tokens")),
            summary=(m.group("summary") or "").strip(),
        ))
    logger.debug("Parsed %s refs from message", len(refs))
    return refs


def generate_ref_text(
    artifact_id: str,
    name: str,
    artifact_type: str,
    version: int,
    token_count: int,
    summary: str = "",
) -> str:
    text = f"[Artifact: {name} | ID: {artifact_id} | Version: v{version} | Type: {artifact_type} | Tokens: {token_count}"
    if summary:
        text += f" | Summary: {summary[:200]}"
    text += "]"
    return text

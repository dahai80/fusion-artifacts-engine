import logging
import re

from fusion_artifacts_engine.token_counter import count_tokens

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


# H7: 以下 section 边界/patch 逻辑从 engine.py 抽出。纯函数无状态，行为不变。


def all_section_bounds(
    content: str, artifact_type: str
) -> list[tuple[int, int, int, str]]:
    # P-1: 一次线性扫预计算所有标题位置 + section 边界，避免 O(n^2)。
    # 返回 (start, end, level, anchor) 列表，end 为下一同级/更高级标题行号。
    lines = content.split("\n")
    headers: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        if artifact_type == "markdown":
            m = re.match(r"^(#{1,6})\s+(.+)$", line)
            if m:
                headers.append((i, len(m.group(1)), m.group(2).strip()))
        elif artifact_type == "code":
            # E4: 多语言 section 检测。覆盖 Python(def/class/async def)
            # JS/TS(function/const/export class/arrow)、Go(func)、Rust(fn/impl/struct/pub)
            # Java(class/public..)。标识符允许 unicode（Python3 非ASCII方法名）。
            m = re.match(
                r"^\s*(?:"
                r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"
                r"|(?:export\s+)?(?:const|let|var)\s+(\w+)\s*="
                r"|(?:export\s+)?(?:default\s+)?class\s+(\w+)"
                r"|(?:async\s+)?def\s+(\w+)"
                r"|class\s+(\w+)"
                r"|(?:pub\s+)?fn\s+(\w+)"
                r"|(?:pub\s+)?(?:struct|enum|trait|impl)\s+(\w+)"
                r"|func(?:\s+\([^)]*\))?\s+(\w+)"
                r"|(?:public|private|protected|static)\s+(?:[\w<>\[\]]+\s+)?(\w+)\s*\("
                r")",
                line,
            )
            if m:
                name = next((g for g in m.groups() if g), None)
                if name:
                    headers.append((i, 1, name))
    bounds: list[tuple[int, int, int, str]] = []
    for idx, (start, level, anchor) in enumerate(headers):
        end = len(lines)
        if artifact_type == "markdown":
            for j in range(idx + 1, len(headers)):
                s2, l2, _ = headers[j]
                if l2 <= level:
                    end = s2
                    break
        else:
            if idx + 1 < len(headers):
                end = headers[idx + 1][0]
        bounds.append((start, end, level, anchor))
    return bounds


def find_section_bounds(
    content: str, anchor: str, artifact_type: str
) -> list[tuple[int, int, int]]:
    # P-1: 复用 all_section_bounds 线性结果，按 anchor 过滤；保持原多重匹配语义。
    anchor = normalize_anchor(anchor)
    return [
        (s, e, lvl)
        for s, e, lvl, anc in all_section_bounds(content, artifact_type)
        if anc == anchor
    ]


def build_sections_with_tokens(content: str, artifact_type: str) -> list[dict]:
    # P-1: 单次线性扫导出所有 section 边界，避免每标题一次 O(n) 扫。
    lines = content.split("\n")
    sections = []
    for start, end, _lvl, anchor in all_section_bounds(content, artifact_type):
        section_text = "\n".join(lines[start:end])
        sections.append(
            {
                "anchor": anchor,
                "tokens": count_tokens(section_text),
            }
        )
    return sections


def replace_section(
    content: str, anchor: str, new_content: str, artifact_type: str
) -> tuple[str, str]:
    matches = find_section_bounds(content, anchor, artifact_type)
    if len(matches) > 1:
        raise ValueError(
            f"Multiple matches for anchor '{anchor}': "
            f"found at lines {[m[0] + 1 for m in matches]}"
        )
    if not matches:
        raise ValueError(f"Anchor '{anchor}' not found in content")
    lines = content.split("\n")
    start, end, _ = matches[0]
    replaced_lines = lines[start:end]
    replaced_content = "\n".join(replaced_lines)
    new_lines = lines[:start] + new_content.split("\n") + lines[end:]
    return "\n".join(new_lines), replaced_content


def delete_section(
    content: str, anchor: str, artifact_type: str
) -> tuple[str, str]:
    matches = find_section_bounds(content, anchor, artifact_type)
    if len(matches) > 1:
        raise ValueError(
            f"Multiple matches for anchor '{anchor}': "
            f"found at lines {[m[0] + 1 for m in matches]}"
        )
    if not matches:
        raise ValueError(f"Anchor '{anchor}' not found in content")
    lines = content.split("\n")
    start, end, _ = matches[0]
    replaced_lines = lines[start:end]
    replaced_content = "\n".join(replaced_lines)
    new_lines = lines[:start] + lines[end:]
    return "\n".join(new_lines), replaced_content

import logging
import re

from fusion_artifacts_engine.token_counter import count_tokens, estimate_tokens

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
    if estimate_tokens(result) <= token_budget and count_tokens(result) <= token_budget:
        return result

    result = _collapse_blank_lines(result)
    if estimate_tokens(result) <= token_budget and count_tokens(result) <= token_budget:
        return result

    result = _remove_decorators(result, artifact_type)
    if estimate_tokens(result) <= token_budget and count_tokens(result) <= token_budget:
        return result

    result = _truncate_sections(result, artifact_type, token_budget)
    return result


def compact_and_count(content: str, artifact_type: str, token_budget: int) -> tuple[str, int]:
    # F3: 压缩 + 精确计数合并，供 engine 卸线程池；压缩结果与 token 数一次返回，
    # 避免 engine 再 count_tokens(compacted_content) 二次全量编码
    compacted = compact_content(content, artifact_type, token_budget)
    return compacted, count_tokens(compacted)


def _remove_comments(content: str, artifact_type: str) -> str:
    lines = content.split("\n")
    out = []
    if artifact_type == "code":
        # L-17: 仅剥整行 # 注释，保留 #! shebang 与 # -*- coding -*- 声明；
        # 跳过三引号字符串内部以 # 开头的行，避免损坏 Python 多行字符串
        in_triple = False
        triple_delims = ('"""', "'''")
        for idx, line in enumerate(lines):
            stripped = line.lstrip()
            for delim in triple_delims:
                count = line.count(delim)
                if count and not in_triple:
                    if count % 2 == 1:
                        in_triple = True
                    out.append(line)
                    break
                elif count and in_triple:
                    if count % 2 == 1:
                        in_triple = False
                    out.append(line)
                    break
            else:
                if in_triple:
                    out.append(line)
                    continue
                if idx == 0 and stripped.startswith("#!"):
                    out.append(line)
                    continue
                if stripped.startswith(("# -*-", "#coding")):
                    out.append(line)
                    continue
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

    # F3: 按行增量计 token + 前缀和，避免每个 section 边界重新 join+全量编码（O(n²)）。
    # 仅在此函数内对每行编码一次；最终返回前用 count_tokens 对拼接结果做一次精确校验。
    line_tokens = [count_tokens(line) for line in lines]
    prefix = [0] * (len(lines) + 1)
    for idx, t in enumerate(line_tokens):
        prefix[idx + 1] = prefix[idx] + t

    if not sections:
        total = prefix[len(lines)]
        if total <= token_budget:
            return "\n".join(lines)
        ratio = token_budget / max(1, total)
        cut = int(len(lines) * ratio)
        candidate = "\n".join(lines[:cut])
        if count_tokens(candidate) <= token_budget:
            return candidate
        while cut > 0 and count_tokens("\n".join(lines[:cut])) > token_budget:
            cut -= 1
        return "\n".join(lines[:cut])

    sections.reverse()
    for sec in sections:
        if prefix[sec["start"]] <= token_budget:
            current = "\n".join(lines[: sec["start"]])
            if count_tokens(current) <= token_budget:
                logger.info(
                    "Truncated sections from anchor='%s' to meet budget=%d",
                    sec["anchor"],
                    token_budget,
                )
                return current
    return "\n".join(lines[:1]) if lines else ""

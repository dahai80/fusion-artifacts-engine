import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_THINKING_RE = re.compile(r"<thinking>\n?(.*?)\n?</thinking>", re.DOTALL)
_DSL_RE = re.compile(r"<dsl>\n?(.*?)\n?</dsl>", re.DOTALL)

_FALLBACK_VISUAL_TYPE = "flow_card"


@dataclass
class ParsedDSL:
    thinking: str | None = None
    dsl: dict | None = None
    valid: bool = False
    errors: list[str] = field(default_factory=list)
    visual_type: str = _FALLBACK_VISUAL_TYPE
    fallback_type: str = "flow_card"


def parse_dsl_json(dsl_str: str) -> tuple[dict | None, list[str]]:
    if not dsl_str or not dsl_str.strip():
        return None, ["empty dsl string"]
    try:
        parsed = json.loads(dsl_str)
    except json.JSONDecodeError as exc:
        logger.debug("dsl json parse fail: %s", exc)
        return None, [f"json parse error: {exc}"]
    if not isinstance(parsed, dict):
        return None, [f"expected dict, got {type(parsed).__name__}"]
    try:
        from fusion_core.dsl_schema import validate_dsl_str

        ok, err = validate_dsl_str(dsl_str)
        if not ok:
            return None, [f"schema violation: {err}"]
    except ImportError:
        logger.warning("fusion_core.dsl_schema unavailable, skipping schema validation")
    except Exception as exc:
        logger.warning("dsl schema validation error: %s", exc)
        return None, [f"schema validation error: {exc}"]
    return parsed, []


def extract_visual_type(dsl_dict: dict | None) -> str:
    if not isinstance(dsl_dict, dict):
        return _FALLBACK_VISUAL_TYPE
    meta = dsl_dict.get("meta") or {}
    if not isinstance(meta, dict):
        return _FALLBACK_VISUAL_TYPE
    vt = meta.get("visual_type")
    if isinstance(vt, str) and vt:
        return vt
    ft = meta.get("fallback_type")
    if isinstance(ft, str) and ft:
        return ft
    return _FALLBACK_VISUAL_TYPE


def get_pipeline_progress(dsl_dict: dict | None) -> list[tuple[float, float]]:
    if not isinstance(dsl_dict, dict):
        return []
    pipeline = dsl_dict.get("pipeline") or []
    if not isinstance(pipeline, list):
        return []
    ranges: list[tuple[float, float]] = []
    for step in pipeline:
        if not isinstance(step, dict):
            continue
        vs = step.get("visual_state") or {}
        if not isinstance(vs, dict):
            continue
        pr = vs.get("progress_range")
        if isinstance(pr, list) and len(pr) == 2:
            try:
                ranges.append((float(pr[0]), float(pr[1])))
            except (TypeError, ValueError):
                ranges.append((0.0, 1.0))
        else:
            ranges.append((0.0, 1.0))
    return ranges


def parse_model_output(raw_text: str) -> ParsedDSL:
    if not raw_text or not raw_text.strip():
        return ParsedDSL(errors=["empty input"])
    thinking_match = _THINKING_RE.search(raw_text)
    thinking = thinking_match.group(1).strip() if thinking_match else None
    if thinking is None:
        logger.debug("no <thinking> tag found in model output")
    dsl_match = _DSL_RE.search(raw_text)
    if dsl_match is None:
        return ParsedDSL(thinking=thinking, errors=["missing dsl tag"])
    dsl_str = dsl_match.group(1).strip()
    dsl_dict, errors = parse_dsl_json(dsl_str)
    if dsl_dict is None:
        return ParsedDSL(thinking=thinking, errors=errors)
    visual_type = extract_visual_type(dsl_dict)
    meta = dsl_dict.get("meta") or {}
    fallback_type = meta.get("fallback_type") or "flow_card"
    parsed = ParsedDSL(
        thinking=thinking,
        dsl=dsl_dict,
        valid=True,
        errors=[],
        visual_type=visual_type,
        fallback_type=fallback_type,
    )
    logger.info(
        "parsed DSL: valid=%s visual_type=%s fallback_type=%s",
        parsed.valid,
        visual_type,
        fallback_type,
    )
    return parsed

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_THINKING_RE = re.compile(r"<thinking>\n?(.*?)\n?</thinking>", re.DOTALL)
_DSL_RE = re.compile(r"<dsl>\n?(.*?)\n?</dsl>", re.DOTALL)

_FALLBACK_VISUAL_TYPE = "flow_card"

# Issue #64/#63 CI: fusion_core.dsl_schema is a monorepo sibling package and
# may be absent in isolated environments (CI installs only this repo). Schema
# validation must never silently degrade to "everything is valid" — a
# malformed DSL would then be marked valid and rendered downstream. This
# fallback validator mirrors the required/enum checks of
# fusion_core.dsl_schema.SOCRATIC_DSL_V3_SCHEMA (kept in sync manually).
_FALLBACK_VISUAL_TYPES = (
    "array_grid",
    "tape_diagram",
    "geometry_2d",
    "isometric_3d",
    "track_timeline",
    "bucket_divider",
    "data_chart",
    "flow_card",
)
_FALLBACK_FALLBACK_TYPES = ("tape_diagram", "flow_card")


def _fallback_validate_dsl(dsl_dict: dict) -> list[str]:
    errors: list[str] = []
    for key in ("meta", "parameters", "entities", "pipeline"):
        if key not in dsl_dict:
            errors.append(f"missing required property: {key}")
    extra = set(dsl_dict) - {"meta", "parameters", "entities", "pipeline"}
    if extra:
        errors.append(f"additional properties not allowed: {sorted(extra)}")
    meta = dsl_dict.get("meta")
    if not isinstance(meta, dict):
        if meta is not None or "meta" not in dsl_dict:
            errors.append("meta must be an object")
    else:
        for key in ("id", "grade", "topic", "visual_type", "fallback_type"):
            if key not in meta:
                errors.append(f"meta: missing required property: {key}")
        meta_extra = set(meta) - {"id", "grade", "topic", "visual_type", "fallback_type"}
        if meta_extra:
            errors.append(f"meta: additional properties not allowed: {sorted(meta_extra)}")
        if meta.get("visual_type") not in _FALLBACK_VISUAL_TYPES:
            errors.append(f"meta.visual_type: {meta.get('visual_type')!r} is not one of {_FALLBACK_VISUAL_TYPES}")
        if meta.get("fallback_type") not in _FALLBACK_FALLBACK_TYPES:
            errors.append(f"meta.fallback_type: {meta.get('fallback_type')!r} is not one of {_FALLBACK_FALLBACK_TYPES}")
    params = dsl_dict.get("parameters")
    if params is not None and not isinstance(params, dict):
        errors.append("parameters must be an object")
    elif isinstance(params, dict):
        for key, val in params.items():
            if not isinstance(val, dict):
                errors.append(f"parameters.{key}: must be an object")
                continue
            for sub in ("label", "value", "unit"):
                if sub not in val:
                    errors.append(f"parameters.{key}: missing required property: {sub}")
    entities = dsl_dict.get("entities")
    if entities is not None and not isinstance(entities, list):
        errors.append("entities must be an array")
    elif isinstance(entities, list):
        for i, ent in enumerate(entities):
            if not isinstance(ent, dict):
                errors.append(f"entities[{i}]: must be an object")
                continue
            for sub in ("id", "type", "label", "properties"):
                if sub not in ent:
                    errors.append(f"entities[{i}]: missing required property: {sub}")
    pipeline = dsl_dict.get("pipeline")
    if pipeline is not None and not isinstance(pipeline, list):
        errors.append("pipeline must be an array")
    elif isinstance(pipeline, list):
        for i, step in enumerate(pipeline):
            if not isinstance(step, dict):
                errors.append(f"pipeline[{i}]: must be an object")
                continue
            for sub in ("step", "title", "formula", "eval", "result_unit", "visual_state"):
                if sub not in step:
                    errors.append(f"pipeline[{i}]: missing required property: {sub}")
            vs = step.get("visual_state")
            if vs is not None and isinstance(vs, dict):
                for sub in ("progress_range", "active_entities", "action"):
                    if sub not in vs:
                        errors.append(f"pipeline[{i}].visual_state: missing required property: {sub}")
                pr = vs.get("progress_range")
                if pr is not None and (not isinstance(pr, list) or len(pr) != 2):
                    errors.append(f"pipeline[{i}].visual_state.progress_range: must be a 2-item array")
    return errors


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
        # #63 CI: fusion_core is a monorepo sibling, not on PyPI — validate
        # with the in-repo fallback instead of silently accepting anything.
        errors = _fallback_validate_dsl(parsed)
        if errors:
            return None, [f"schema violation: {'; '.join(errors)}"]
        logger.debug("validated with in-repo fallback validator (fusion_core absent)")
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

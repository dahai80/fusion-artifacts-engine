import logging

from fusion_artifacts_engine.dsl_parser import ParsedDSL, extract_visual_type
from fusion_artifacts_engine.visual_error_boundary import VisualErrorBoundary
from fusion_artifacts_engine.visual_primitives import (
    FlowCardCompiler,
    TapeDiagramCompiler,
    get_compiler,
)

logger = logging.getLogger(__name__)


class VisualEngineDispatcher:
    # #59: 按 dsl.meta.visual_type 路由到对应 compiler，3 级兜底链：
    #   Level 1: 主 compiler（如 isometric_3d → Isometric3DCompiler）
    #   Level 2: TapeDiagramCompiler（主 compiler 抛异常或缺参）
    #   Level 3: FlowCardCompiler（100% 保证渲染）
    # dsl.meta.fallback_type 可覆盖 Level 2（tape_diagram 或 flow_card）。

    def __init__(self):
        self._tape = TapeDiagramCompiler()
        self._flow = FlowCardCompiler()
        self._boundary = VisualErrorBoundary(self._flow)

    def dispatch(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        visual_type = extract_visual_type(dsl_dict)
        fallback_type = "tape_diagram"
        if isinstance(dsl_dict, dict):
            meta = dsl_dict.get("meta") or {}
            if isinstance(meta, dict):
                ft = meta.get("fallback_type")
                if isinstance(ft, str) and ft in ("tape_diagram", "flow_card"):
                    fallback_type = ft
        level2 = self._flow if fallback_type == "flow_card" else self._tape
        logger.info(
            "dispatch visual_type=%s fallback_type=%s progress=%.2f",
            visual_type,
            fallback_type,
            progress,
        )
        primary = get_compiler(visual_type)
        try:
            html_out = primary.compile(dsl_dict, progress)
            if html_out and isinstance(html_out, str):
                return html_out
            logger.warning(
                "primary compiler %s returned empty, fallback to %s",
                primary.__class__.__name__,
                fallback_type,
            )
        except Exception as exc:
            logger.warning(
                "primary compiler %s threw: %s, fallback to %s",
                primary.__class__.__name__,
                exc,
                fallback_type,
            )
        try:
            html_out = level2.compile(dsl_dict, progress)
            if html_out and isinstance(html_out, str):
                return html_out
            logger.warning(
                "level2 compiler %s returned empty, fallback to FlowCard",
                level2.__class__.__name__,
            )
        except Exception as exc:
            logger.warning(
                "level2 compiler %s threw: %s, fallback to FlowCard",
                level2.__class__.__name__,
                exc,
            )
        return self._boundary.render(self._flow, dsl_dict, progress)

    def dispatch_parsed(self, parsed: ParsedDSL, progress: float = 1.0) -> str:
        if not parsed.valid or parsed.dsl is None:
            logger.info("dispatch_parsed: invalid DSL, FlowCard placeholder")
            return self._flow.compile(parsed.dsl, progress)
        return self.dispatch(parsed.dsl, progress)

    def render(
        self,
        raw_text: str | None = None,
        dsl: dict | None = None,
        progress: float = 1.0,
    ) -> dict:
        # #58/#59/#60: 可视化渲染入口。接受原始模型输出或已解析 DSL dict，
        # 返回 {html, visual_type, valid, errors, [thinking]}。永远返回 HTML（FlowCard 兜底）。
        from fusion_artifacts_engine.dsl_parser import parse_model_output
        from fusion_artifacts_engine.visual_primitives import VALID_VISUAL_TYPES

        if dsl is not None:
            visual_type = (
                dsl.get("meta", {}).get("visual_type", "flow_card")
                if isinstance(dsl, dict)
                else "flow_card"
            )
            valid = isinstance(dsl, dict)
            errors: list[str] = [] if valid else ["invalid dsl dict"]
            thinking = None
        elif raw_text:
            parsed = parse_model_output(raw_text)
            visual_type = parsed.visual_type
            valid = parsed.valid
            errors = parsed.errors
            thinking = parsed.thinking
            dsl = parsed.dsl
        else:
            return {
                "html": self.dispatch(None, progress),
                "visual_type": "flow_card",
                "valid": False,
                "errors": ["empty input"],
            }
        if visual_type not in VALID_VISUAL_TYPES:
            logger.warning("render: unknown visual_type %r, fallback", visual_type)
        html_out = self.dispatch(dsl, progress)
        logger.info(
            "render: visual_type=%s valid=%s progress=%.2f html_len=%d",
            visual_type,
            valid,
            progress,
            len(html_out),
        )
        result: dict = {
            "html": html_out,
            "visual_type": visual_type,
            "valid": valid,
            "errors": errors,
        }
        if thinking is not None:
            result["thinking"] = thinking
        return result

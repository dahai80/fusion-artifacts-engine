import logging

logger = logging.getLogger(__name__)


class VisualErrorBoundary:
    # #59: 包裹 compiler.compile() 调用，捕获任何异常，
    # 记录 DSL 元数据（visual_type / meta.id），返回 fallback compiler 输出。
    # 绝不向调用方传播异常——100% 返回 HTML 字符串。

    def __init__(self, fallback_compiler):
        self._fallback = fallback_compiler

    def render(self, compiler, dsl_dict: dict | None, progress: float = 1.0) -> str:
        meta_id = ""
        vt = "unknown"
        if isinstance(dsl_dict, dict):
            meta = dsl_dict.get("meta") or {}
            if isinstance(meta, dict):
                meta_id = meta.get("id") or ""
                vt = meta.get("visual_type") or "unknown"
        try:
            html_out = compiler.compile(dsl_dict, progress)
            if html_out and isinstance(html_out, str):
                return html_out
            logger.warning(
                "compiler %s returned empty/non-str for meta.id=%s visual_type=%s, fallback",
                compiler.__class__.__name__,
                meta_id,
                vt,
            )
        except Exception as exc:
            logger.warning(
                "compiler %s threw for meta.id=%s visual_type=%s: %s, fallback",
                compiler.__class__.__name__,
                meta_id,
                vt,
                exc,
            )
        try:
            return self._fallback.compile(dsl_dict, progress)
        except Exception as exc2:
            logger.error(
                "fallback compiler %s also threw: %s, returning bare placeholder",
                self._fallback.__class__.__name__,
                exc2,
            )
            return (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<title>render error</title></head>"
                "<body><p>visual rendering failed</p></body></html>"
            )

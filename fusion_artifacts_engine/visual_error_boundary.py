import logging

logger = logging.getLogger(__name__)


class VisualErrorBoundary:
    # #59: wraps compiler.compile() calls, catches any exception,
    # logs the DSL metadata (visual_type / meta.id), and returns the fallback compiler output.
    # Never propagates exceptions to callers — always returns an HTML string (100%).

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

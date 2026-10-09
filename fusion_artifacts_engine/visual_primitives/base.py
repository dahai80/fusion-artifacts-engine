import html
import logging

logger = logging.getLogger(__name__)


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=True)


class BaseCompiler:
    # 8 个可视化基元的公共基类。每个子类实现 compile() 返回自包含 HTML 字符串。
    # progress [0.0,1.0] 驱动步骤回放；active_entities 按 pipeline step 高亮。

    visual_type: str = "base"

    def __init__(self):
        pass

    def compile(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        raise NotImplementedError

    def _meta(self, dsl_dict: dict | None) -> dict:
        if not isinstance(dsl_dict, dict):
            return {}
        meta = dsl_dict.get("meta") or {}
        return meta if isinstance(meta, dict) else {}

    def _parameters(self, dsl_dict: dict | None) -> dict:
        if not isinstance(dsl_dict, dict):
            return {}
        params = dsl_dict.get("parameters") or {}
        return params if isinstance(params, dict) else {}

    def _entities(self, dsl_dict: dict | None) -> list[dict]:
        if not isinstance(dsl_dict, dict):
            return []
        ents = dsl_dict.get("entities") or []
        return ents if isinstance(ents, list) else []

    def _pipeline(self, dsl_dict: dict | None) -> list[dict]:
        if not isinstance(dsl_dict, dict):
            return []
        pipe = dsl_dict.get("pipeline") or []
        return pipe if isinstance(pipe, list) else []

    def _active_step(self, dsl_dict: dict | None, progress: float) -> dict | None:
        pipeline = self._pipeline(dsl_dict)
        if not pipeline:
            return None
        p = max(0.0, min(1.0, float(progress)))
        for step in pipeline:
            vs = step.get("visual_state") or {}
            pr = vs.get("progress_range") or [0.0, 1.0]
            if len(pr) == 2 and pr[0] <= p <= pr[1]:
                return step
        return pipeline[-1]

    def _active_entities(self, dsl_dict: dict | None, progress: float) -> list[str]:
        step = self._active_step(dsl_dict, progress)
        if step is None:
            return []
        vs = step.get("visual_state") or {}
        ae = vs.get("active_entities") or []
        return ae if isinstance(ae, list) else []

    def _title(self, dsl_dict: dict | None) -> str:
        meta = self._meta(dsl_dict)
        return _esc(meta.get("id") or meta.get("topic") or "visual")

    def _html_wrap(self, title: str, body: str, style: str = "", script: str = "") -> str:
        style_tag = f"<style>{style}</style>" if style else ""
        script_tag = f"<script>{script}</script>" if script else ""
        return (
            f"<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title>{style_tag}</head>"
            f"<body>{body}{script_tag}</body></html>"
        )

import logging

from fusion_artifacts_engine.visual_primitives.base import BaseCompiler, _esc

logger = logging.getLogger(__name__)


class TapeDiagramCompiler(BaseCompiler):
    # 2-6年级 和差倍比/分数。DSL → HTML+SVG 条带/分段图。
    # 核心兜底基元——必须处理任意数量关系。
    # 动画：stretch / cut / highlight segments。

    visual_type = "tape_diagram"

    def compile(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        params = self._parameters(dsl_dict)
        segments: list[tuple[str, float]] = []
        for key, val in params.items():
            if not isinstance(val, dict):
                continue
            v = val.get("value")
            if isinstance(v, (int, float)) and v >= 0:
                label = val.get("label") or key
                segments.append((label, float(v)))
        if not segments:
            segments = [("A", 1.0), ("B", 1.0)]
        total = sum(v for _, v in segments) or 1.0
        active = set(self._active_entities(dsl_dict, progress))
        bar_w = 600
        bar_h = 60
        x = 0
        parts = []
        colors = ["#4dabf7", "#69db7c", "#ffd43b", "#ff6b6b", "#da77f2", "#ffa94d"]
        for i, (label, val) in enumerate(segments):
            seg_w = (val / total) * bar_w
            color = colors[i % len(colors)]
            ent_id = f"seg_{i}"
            opacity = "1.0" if ent_id in active else "0.7"
            parts.append(
                f"<rect x='{x:.1f}' y='0' width='{seg_w:.1f}' height='{bar_h}' "
                f"fill='{color}' opacity='{opacity}' class='seg' data-id='{ent_id}'/>"
            )
            if seg_w > 30:
                lbl = _esc(label)
                parts.append(
                    f"<text x='{x + seg_w / 2:.1f}' y='{bar_h // 2}' text-anchor='middle' "
                    f"dy='.35em' font-size='13' fill='#fff'>{lbl}: {val:g}</text>"
                )
            x += seg_w
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{bar_w}' height='{bar_h + 40}' "
            f"viewBox='0 0 {bar_w} {bar_h + 40}'>"
            f"<g transform='translate(0,10)'>" + "".join(parts) + f"</g>"
            f"<text x='{bar_w // 2}' y='{bar_h + 30}' text-anchor='middle' "
            f"font-size='13' fill='#333'>总计: {total:g}</text>" + "</svg>"
        )
        style = ".seg{transition:opacity .3s}"
        return self._html_wrap(self._title(dsl_dict), svg, style)

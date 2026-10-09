import logging

from fusion_artifacts_engine.visual_primitives.base import BaseCompiler

logger = logging.getLogger(__name__)


class ArrayGridCompiler(BaseCompiler):
    # 1-2年级 计数/阵列/乘法。DSL → HTML+SVG 点阵/阵列网格。
    # 动画：combine / circle / pre-allocate。

    visual_type = "array_grid"

    def compile(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        params = self._parameters(dsl_dict)
        count = 0
        rows = 1
        cols = 1
        for key, val in params.items():
            if not isinstance(val, dict):
                continue
            label = (val.get("label") or key).lower()
            v = val.get("value")
            if not isinstance(v, (int, float)):
                continue
            if "count" in label or "总数" in label or "total" in label:
                count = int(v)
            elif "row" in label or "行" in label:
                rows = max(1, int(v))
            elif "col" in label or "列" in label:
                cols = max(1, int(v))
        if count <= 0:
            count = rows * cols if rows * cols > 0 else 12
        if rows * cols <= 0:
            rows, cols = 1, count
        total = rows * cols
        shown = min(count, total)
        active = set(self._active_entities(dsl_dict, progress))
        cell = 44
        gap = 6
        w = cols * (cell + gap) + gap
        h = rows * (cell + gap) + gap + 40
        dots = []
        idx = 0
        for r in range(rows):
            for c in range(cols):
                if idx >= shown:
                    break
                cx = gap + c * (cell + gap) + cell // 2
                cy = gap + r * (cell + gap) + cell // 2
                ent_id = f"dot_{idx}"
                fill = "#ff6b6b" if ent_id in active else "#4dabf7"
                dots.append(
                    f"<circle cx='{cx}' cy='{cy}' r='16' fill='{fill}' "
                    f"class='dot' data-id='{ent_id}'/>"
                )
                idx += 1
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' "
            f"viewBox='0 0 {w} {h}'>"
            + "".join(dots)
            + f"<text x='{w // 2}' y='{h - 8}' text-anchor='middle' "
            f"font-size='14' fill='#333'>共 {shown} 个</text>" + "</svg>"
        )
        style = ".dot{transition:fill .3s}@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}"
        return self._html_wrap(self._title(dsl_dict), svg, style)

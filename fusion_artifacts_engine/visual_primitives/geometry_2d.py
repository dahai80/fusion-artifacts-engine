import logging

from fusion_artifacts_engine.visual_primitives.base import BaseCompiler, _esc

logger = logging.getLogger(__name__)


class Geometry2DCompiler(BaseCompiler):
    # 3-5年级 周长/面积/拼剪。DSL → HTML+SVG 2D 画布。
    # 动画：polygon draw / translate / rotate / overlap。

    visual_type = "geometry_2d"

    def compile(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        entities = self._entities(dsl_dict)
        active = set(self._active_entities(dsl_dict, progress))
        w = 600
        h = 400
        shapes = []
        colors = ["#4dabf7", "#69db7c", "#ffd43b", "#ff6b6b"]
        for i, ent in enumerate(entities):
            if not isinstance(ent, dict):
                continue
            eid = ent.get("id") or f"ent_{i}"
            etype = ent.get("type") or "rect"
            props = ent.get("properties") or {}
            if not isinstance(props, dict):
                props = {}
            color = colors[i % len(colors)]
            opacity = "0.85" if eid in active else "0.5"
            stroke = "#333" if eid in active else "#999"
            sw = "3" if eid in active else "1"
            et = etype.lower()
            if "rect" in et or "square" in et:
                rw = float(props.get("width") or 80)
                rh = float(props.get("height") or 60)
                rx = float(props.get("x") or 50 + i * 80)
                ry = float(props.get("y") or 50)
                shapes.append(
                    f"<rect x='{rx}' y='{ry}' width='{rw}' height='{rh}' "
                    f"fill='{color}' fill-opacity='{opacity}' stroke='{stroke}' "
                    f"stroke-width='{sw}' class='shape' data-id='{_esc(eid)}'/>"
                )
            elif "circle" in et:
                cx = float(props.get("cx") or 100 + i * 80)
                cy = float(props.get("cy") or 100)
                r = float(props.get("r") or 40)
                shapes.append(
                    f"<circle cx='{cx}' cy='{cy}' r='{r}' fill='{color}' "
                    f"fill-opacity='{opacity}' stroke='{stroke}' stroke-width='{sw}' "
                    f"class='shape' data-id='{_esc(eid)}'/>"
                )
            elif "triangle" in et or "polygon" in et:
                pts = props.get("points") or "50,200 150,100 250,200"
                shapes.append(
                    f"<polygon points='{_esc(str(pts))}' fill='{color}' "
                    f"fill-opacity='{opacity}' stroke='{stroke}' stroke-width='{sw}' "
                    f"class='shape' data-id='{_esc(eid)}'/>"
                )
            else:
                rw = float(props.get("width") or 80)
                rh = float(props.get("height") or 60)
                rx = float(props.get("x") or 50 + i * 80)
                ry = float(props.get("y") or 50)
                shapes.append(
                    f"<rect x='{rx}' y='{ry}' width='{rw}' height='{rh}' "
                    f"fill='{color}' fill-opacity='{opacity}' stroke='{stroke}' "
                    f"stroke-width='{sw}' class='shape' data-id='{_esc(eid)}'/>"
                )
        if not shapes:
            shapes.append(
                "<rect x='50' y='50' width='200' height='120' fill='#4dabf7' "
                "fill-opacity='0.5' stroke='#999' stroke-width='1'/>"
            )
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' "
            f"viewBox='0 0 {w} {h}'>" + "".join(shapes) + "</svg>"
        )
        style = ".shape{transition:all .3s}"
        return self._html_wrap(self._title(dsl_dict), svg, style)

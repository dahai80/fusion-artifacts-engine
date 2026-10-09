import logging

from fusion_artifacts_engine.visual_primitives.base import BaseCompiler, _esc

logger = logging.getLogger(__name__)


class TrackTimelineCompiler(BaseCompiler):
    # 4-6年级 行程/追及/流水。DSL → HTML+SVG 运动轨道。
    # 动画：多体匀速运动 / 位置追踪。

    visual_type = "track_timeline"

    def compile(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        entities = self._entities(dsl_dict)
        active = set(self._active_entities(dsl_dict, progress))
        track_len = 500
        track_h = 200
        lanes = []
        if not entities:
            entities = [
                {"id": "a", "type": "runner", "label": "甲", "properties": {"speed": 60}},
                {"id": "b", "type": "runner", "label": "乙", "properties": {"speed": 40}},
            ]
        colors = ["#4dabf7", "#ff6b6b", "#69db7c", "#ffd43b"]
        lane_h = 40
        for i, ent in enumerate(entities):
            if not isinstance(ent, dict):
                continue
            eid = ent.get("id") or f"ent_{i}"
            label = _esc(ent.get("label") or eid)
            props = ent.get("properties") or {}
            if not isinstance(props, dict):
                props = {}
            speed = float(props.get("speed") or props.get("v") or 10)
            start = float(props.get("start") or 0)
            pos = start + speed * progress * 3
            pos = min(pos, track_len - 20)
            y = i * lane_h + 20
            color = colors[i % len(colors)]
            opacity = "1.0" if eid in active else "0.6"
            lanes.append(
                f"<line x1='10' y1='{y}' x2='{track_len}' y2='{y}' "
                f"stroke='#ddd' stroke-width='2'/>"
                f"<circle cx='{pos + 10:.1f}' cy='{y}' r='12' fill='{color}' "
                f"opacity='{opacity}' class='runner' data-id='{_esc(eid)}'/>"
                f"<text x='{pos + 10:.1f}' y='{y - 16}' text-anchor='middle' "
                f"font-size='12' fill='#333'>{label}</text>"
            )
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{track_len + 20}' "
            f"height='{track_h}' viewBox='0 0 {track_len + 20} {track_h}'>"
            + "".join(lanes)
            + f"<text x='{track_len // 2}' y='{track_h - 5}' text-anchor='middle' "
            f"font-size='12' fill='#666'>progress: {progress:.1f}</text>" + "</svg>"
        )
        style = ".runner{transition:cx .3s,opacity .3s}"
        return self._html_wrap(self._title(dsl_dict), svg, style)

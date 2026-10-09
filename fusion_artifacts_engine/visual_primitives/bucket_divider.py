import logging

from fusion_artifacts_engine.visual_primitives.base import BaseCompiler

logger = logging.getLogger(__name__)


class BucketDividerCompiler(BaseCompiler):
    # 4-6年级 隔板/抽屉/容斥。DSL → HTML+SVG 桶/分区。
    # 动画：element fly-in / divider drag。

    visual_type = "bucket_divider"

    def compile(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        params = self._parameters(dsl_dict)
        entities = self._entities(dsl_dict)
        active = set(self._active_entities(dsl_dict, progress))
        bucket_count = 0
        element_count = 0
        for key, val in params.items():
            if not isinstance(val, dict):
                continue
            label = (val.get("label") or key).lower()
            v = val.get("value")
            if not isinstance(v, (int, float)):
                continue
            if "bucket" in label or "桶" in label or "group" in label:
                bucket_count = max(1, int(v))
            elif "element" in label or "元素" in label or "item" in label:
                element_count = max(0, int(v))
        if bucket_count <= 0:
            bucket_count = min(4, max(1, len(entities))) if entities else 3
        if element_count <= 0:
            element_count = 8
        shown = int(element_count * progress)
        bw = 120
        gap = 20
        w = bucket_count * (bw + gap) + gap
        h = 200
        buckets = []
        per = max(1, bucket_count)
        for b in range(bucket_count):
            bx = gap + b * (bw + gap)
            buckets.append(
                f"<rect x='{bx}' y='40' width='{bw}' height='120' rx='8' "
                f"fill='#f8f9fa' stroke='#adb5bd' stroke-width='2' "
                f"class='bucket' data-id='bucket_{b}'/>"
            )
            for e in range(shown // per + (1 if b < shown % per else 0)):
                ex = bx + 20 + (e % 4) * 25
                ey = 60 + (e // 4) * 25
                color = "#4dabf7" if f"bucket_{b}" not in active else "#ff6b6b"
                buckets.append(f"<circle cx='{ex}' cy='{ey}' r='9' fill='{color}' class='item'/>")
            buckets.append(
                f"<text x='{bx + bw // 2}' y='180' text-anchor='middle' "
                f"font-size='13' fill='#495057'>桶 {b + 1}</text>"
            )
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' "
            f"viewBox='0 0 {w} {h}'>"
            + "".join(buckets)
            + f"<text x='{w // 2}' y='20' text-anchor='middle' font-size='13' "
            f"fill='#333'>{shown}/{element_count} 个元素分配到 {bucket_count} 桶</text>" + "</svg>"
        )
        style = ".bucket{transition:stroke .3s}.item{transition:fill .3s}"
        return self._html_wrap(self._title(dsl_dict), svg, style)

import json
import logging

from fusion_artifacts_engine.visual_primitives.base import BaseCompiler

logger = logging.getLogger(__name__)

_CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"


class DataChartCompiler(BaseCompiler):
    # 4-5年级 统计/条形/折线。DSL → HTML+Chart.js。
    # 动画：bar stretch / line draw。

    visual_type = "data_chart"

    def compile(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        params = self._parameters(dsl_dict)
        pipeline = self._pipeline(dsl_dict)
        labels: list[str] = []
        values: list[float] = []
        chart_type = "bar"
        meta = self._meta(dsl_dict)
        vt_hint = (meta.get("topic") or "").lower()
        if "折线" in vt_hint or "line" in vt_hint:
            chart_type = "line"
        for key, val in params.items():
            if not isinstance(val, dict):
                continue
            v = val.get("value")
            if isinstance(v, (int, float)):
                labels.append(val.get("label") or key)
                values.append(float(v))
        if not labels:
            for i, step in enumerate(pipeline):
                if isinstance(step, dict):
                    labels.append(step.get("title") or f"步骤{i + 1}")
                    values.append(float(i + 1))
        if not labels:
            labels = ["A", "B", "C", "D"]
            values = [10, 20, 15, 25]
        scaled = [round(v * progress, 2) for v in values]
        data_json = json.dumps(
            {"labels": labels, "values": scaled, "chart_type": chart_type},
            ensure_ascii=False,
        )
        body = "<div style='width:600px;height:400px;'><canvas id='data-chart'></canvas></div>"
        script = (
            f"<script src='{_CHARTJS_CDN}'></script>"
            f"<script>(function(){{"
            f"var D={data_json};"
            "if(typeof Chart==='undefined'){"
            "document.getElementById('data-chart').parentNode.innerHTML="
            "'<p>Chart.js failed to load</p>';return;}"
            "new Chart(document.getElementById('data-chart').getContext('2d'),{"
            "type:D.chart_type,"
            "data:{labels:D.labels,"
            "datasets:[{label:'数据',data:D.values,"
            "backgroundColor:'#4dabf7',borderColor:'#4dabf7',"
            "borderWidth:2}]},"
            "options:{responsive:true,maintainAspectRatio:false}"
            "});"
            "})();</script>"
        )
        return self._html_wrap(self._title(dsl_dict), body, "", script)

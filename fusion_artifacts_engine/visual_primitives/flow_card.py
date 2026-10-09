import logging

from fusion_artifacts_engine.visual_primitives.base import BaseCompiler, _esc

logger = logging.getLogger(__name__)


class FlowCardCompiler(BaseCompiler):
    # 全学段 纯算式/方程/兜底。DSL → HTML+CSS 步骤卡片。
    # 通用兜底基元——100% 渲染保证，任意输入（含空 pipeline）都不抛异常。
    # 动画：formula highlight / equation transformation。

    visual_type = "flow_card"

    def compile(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        try:
            pipeline = self._pipeline(dsl_dict)
            meta = self._meta(dsl_dict)
            params = self._parameters(dsl_dict)
        except Exception:
            pipeline, meta, params = [], {}, {}
        title_text = _esc(meta.get("topic") or meta.get("id") or "解题步骤")
        if not pipeline:
            body = (
                f"<div class='flow-container'>"
                f"<h2 class='flow-title'>{title_text}</h2>"
                f"<div class='flow-card placeholder'>"
                f"<p class='formula'>暂无解题步骤数据</p>"
                f"</div></div>"
            )
            style = self._style()
            return self._html_wrap(title_text, body, style)
        cards = []
        p = max(0.0, min(1.0, float(progress)))
        for i, step in enumerate(pipeline):
            if not isinstance(step, dict):
                continue
            step_num = step.get("step") or i + 1
            step_title = _esc(step.get("title") or f"步骤 {step_num}")
            formula = _esc(step.get("formula") or "")
            eval_str = _esc(step.get("eval") or "")
            result_unit = _esc(step.get("result_unit") or "")
            vs = step.get("visual_state") or {}
            pr = vs.get("progress_range") or [0.0, 1.0]
            is_active = len(pr) == 2 and pr[0] <= p <= pr[1]
            active_class = " active" if is_active else ""
            hidden_class = " hidden" if (len(pr) == 2 and p < pr[0]) else ""
            cards.append(
                f"<div class='flow-card{active_class}{hidden_class}'>"
                f"<div class='step-num'>{step_num}</div>"
                f"<div class='step-body'>"
                f"<h3>{step_title}</h3>"
                f"<p class='formula'>{formula}</p>"
                + (f"<p class='eval'>= {eval_str}</p>" if eval_str else "")
                + (f"<p class='result'>结果单位: {result_unit}</p>" if result_unit else "")
                + "</div></div>"
            )
        param_rows = []
        for key, val in params.items():
            if not isinstance(val, dict):
                continue
            pl = _esc(val.get("label") or key)
            pv = val.get("value")
            pu = _esc(val.get("unit") or "")
            param_rows.append(f"<tr><td>{pl}</td><td>{pv}</td><td>{pu}</td></tr>")
        params_html = ""
        if param_rows:
            params_html = (
                "<table class='params'><tr><th>量</th><th>值</th><th>单位</th></tr>"
                + "".join(param_rows)
                + "</table>"
            )
        body = (
            f"<div class='flow-container'>"
            f"<h2 class='flow-title'>{title_text}</h2>"
            f"{params_html}"
            f"<div class='flow-cards'>" + "".join(cards) + "</div></div>"
        )
        return self._html_wrap(title_text, body, self._style())

    def _style(self) -> str:
        return (
            ".flow-container{font-family:system-ui,sans-serif;max-width:680px;"
            "margin:0 auto;padding:16px}"
            ".flow-title{text-align:center;color:#1a1a2e;margin-bottom:16px}"
            ".flow-cards{display:flex;flex-direction:column;gap:12px}"
            ".flow-card{display:flex;gap:12px;background:#fff;border:1px solid #e0e0e0;"
            "border-radius:8px;padding:12px;transition:all .3s}"
            ".flow-card.active{border-color:#4dabf7;box-shadow:0 2px 8px rgba(77,171,247,.2)}"
            ".flow-card.hidden{opacity:.4}"
            ".step-num{flex-shrink:0;width:32px;height:32px;border-radius:50%;"
            "background:#4dabf7;color:#fff;display:flex;align-items:center;"
            "justify-content:center;font-weight:bold}"
            ".step-body h3{margin:0 0 8px;font-size:15px;color:#333}"
            ".formula{font-family:monospace;font-size:16px;color:#1a1a2e;margin:4px 0}"
            ".eval{font-family:monospace;font-size:15px;color:#69db7c;margin:4px 0}"
            ".result{font-size:13px;color:#666;margin:4px 0}"
            ".params{width:100%;border-collapse:collapse;margin-bottom:16px}"
            ".params th,.params td{border:1px solid #ddd;padding:6px 10px;text-align:center}"
            ".params th{background:#f8f9fa}"
        )

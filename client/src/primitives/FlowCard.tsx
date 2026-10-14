// Port of visual_primitives/flow_card.py (issue #63).
// 全学段 纯算式/方程/兜底。步骤卡片，100% 渲染保证——任意输入不抛异常。

import { clamp01, metaOf, parametersOf, pipelineOf, titleOf } from "../helpers";
import type { PrimitiveProps } from "./shared";

export function FlowCard({ dsl, progress }: PrimitiveProps) {
  const pipeline = pipelineOf(dsl);
  const meta = metaOf(dsl);
  const params = parametersOf(dsl);
  const titleText = String(meta.topic ?? meta.id ?? "解题步骤");

  let cards: React.ReactNode;
  if (pipeline.length === 0) {
    cards = (
      <div className="fae-flow-card fae-placeholder">
        <p className="fae-formula">暂无解题步骤数据</p>
      </div>
    );
  } else {
    const p = clamp01(progress);
    cards = pipeline.map((step, i) => {
      if (!step || typeof step !== "object") return null;
      const stepNum = step.step ?? i + 1;
      const pr = step.visual_state?.progress_range;
      const isActive = Array.isArray(pr) && pr.length === 2 && pr[0] <= p && p <= pr[1];
      const hidden = Array.isArray(pr) && pr.length === 2 && p < pr[0];
      const cls = `fae-flow-card${isActive ? " fae-active" : ""}${hidden ? " fae-hidden" : ""}`;
      return (
        <div key={`${stepNum}-${i}`} className={cls}>
          <div className="fae-step-num">{stepNum}</div>
          <div className="fae-step-body">
            <h3>{step.title ?? `步骤 ${stepNum}`}</h3>
            <p className="fae-formula">{step.formula ?? ""}</p>
            {step.eval ? <p className="fae-eval">= {step.eval}</p> : null}
            {step.result_unit ? <p className="fae-result">结果单位: {step.result_unit}</p> : null}
          </div>
        </div>
      );
    });
  }

  const paramRows = Object.entries(params).map(([key, val]) => {
    if (!val || typeof val !== "object") return null;
    return (
      <tr key={key}>
        <td>{val.label ?? key}</td>
        <td>{val.value}</td>
        <td>{val.unit ?? ""}</td>
      </tr>
    );
  });

  return (
    <div className="fae-primitive fae-flow-container">
      <h2 className="fae-title">{titleText}</h2>
      {paramRows.length > 0 ? (
        <table className="fae-params">
          <thead>
            <tr>
              <th>量</th>
              <th>值</th>
              <th>单位</th>
            </tr>
          </thead>
          <tbody>{paramRows}</tbody>
        </table>
      ) : null}
      <div className="fae-flow-cards">{cards}</div>
    </div>
  );
}

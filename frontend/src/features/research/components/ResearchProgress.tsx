/** 研究进度仅显示执行步骤和简短结果，不展示模型内部推理。 */
import { CheckCircle2, Circle, CircleAlert, LoaderCircle } from "lucide-react";
import type { ResearchStep, StepStatus } from "../contracts/session";
import styles from "../pages/ResearchSession.module.css";
const labels: Record<StepStatus, string> = { queued: "待执行", running: "执行中", succeeded: "已完成", failed: "失败", skipped: "已跳过" };
/** 没有步骤时说明预期职责，不把静态流程画成已经执行的任务。 */
export function ResearchProgress({ steps }: { steps: readonly ResearchStep[] }) {
  return <details className={styles.progress}><summary>研究进度 <span>{steps.length ? `${steps.filter((step) => step.status === "succeeded").length} / ${steps.length} 项完成` : "尚未开始"}</span></summary>
    {!steps.length ? <p className={styles.progressEmpty}>开始研究后，这里将展示条件整理、数据分析和结论整理的实际进度。</p> : <ol>{steps.map((step) => {
      const Icon = step.status === "succeeded" ? CheckCircle2 : step.status === "failed" ? CircleAlert : step.status === "running" ? LoaderCircle : Circle;
      return <li key={step.id}><Icon size={17} aria-hidden="true" /><div><strong>{step.title}</strong>{step.summary && <p>{step.summary}</p>}</div><span>{labels[step.status]}{step.durationMs !== undefined ? ` · ${(step.durationMs / 1000).toFixed(1)}s` : ""}</span></li>;
    })}</ol>}
  </details>;
}

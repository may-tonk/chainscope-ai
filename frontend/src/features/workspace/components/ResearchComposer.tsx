/** 受控研究输入区：草稿属于页面，提交能力由真实适配器提供，不直接发请求。 */
import { ArrowUpRight, CornerDownLeft, Layers3, X } from "lucide-react";
import { useId, useState, type FormEvent, type Ref } from "react";
import { Button } from "../../../shared/ui/Button";
import type { ResearchDraft, ResearchExecution } from "../types/research";
import styles from "./ResearchComposer.module.css";
interface Props { draft: ResearchDraft; onChange: (draft: ResearchDraft) => void; execution: ResearchExecution; inputRef?: Ref<HTMLTextAreaElement>; submitLabel?: string; executionHint?: string }
/** 对象以链和完整地址标识；输入区不擅自解析时间、身份或分析方法。 */
export function ResearchComposer({ draft, onChange, execution, inputRef, submitLabel = "开始研究", executionHint }: Props) {
  const inputId = useId();
  const hintId = useId();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const available = execution.status === "available";
  /** 只调用注入的真实服务；失败保留草稿，并使用安全提示替代原始异常文本。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (execution.status !== "available" || submitting || !draft.question.trim()) return;
    setSubmitting(true);
    setError(null);
    try { await execution.submit({ ...draft, question: draft.question.trim() }); }
    catch { setError("暂时无法提交研究，问题已保留，请稍后重试。"); }
    finally { setSubmitting(false); }
  }
  return (
    <form className={styles.composer} onSubmit={(event) => void handleSubmit(event)} aria-label="研究问题输入区">
      <div className={styles.topline}><label htmlFor={inputId}>你想研究什么？</label><span>从问题出发，沿证据深入</span></div>
      {draft.contexts.length > 0 && <ul className={styles.contexts} aria-label="已引用的研究对象">{draft.contexts.map((context) => (
        <li key={`${context.chainId}:${context.address}`} className={styles.context}>
          <Layers3 size={14} aria-hidden="true" /><span title={context.address}>{context.symbol} · 链 {context.chainId}</span>
          <button type="button" aria-label={`移除 ${context.symbol} 研究对象`} disabled={submitting} onClick={() => onChange({ ...draft, contexts: draft.contexts.filter((item) => item.chainId !== context.chainId || item.address !== context.address) })}><X size={14} aria-hidden="true" /></button>
        </li>
      ))}</ul>}
      <textarea id={inputId} ref={inputRef} rows={4} value={draft.question} disabled={submitting} aria-describedby={hintId}
        placeholder="描述你的研究问题，或粘贴代币合约、钱包地址、交易哈希…"
        onChange={(event) => { onChange({ ...draft, question: event.target.value }); setError(null); }} />
      <div className={styles.toolbar}>
        <div className={styles.tools}><span className={styles.chain}><span aria-hidden="true" />BNB Smart Chain</span>
          <button type="button" className={styles.clear} disabled={submitting || (!draft.question && !draft.contexts.length)} onClick={() => { onChange({ question: "", contexts: [] }); setError(null); }}>清空</button>
        </div>
        <Button type="submit" disabled={!available || !draft.question.trim()} loading={submitting}>{submitLabel} <ArrowUpRight size={17} aria-hidden="true" /></Button>
      </div>
      <div className={styles.footnote} id={hintId}><span><CornerDownLeft size={13} aria-hidden="true" /> Enter 换行</span><span>{executionHint ?? (available ? "按问题需要补充对象和时间" : "研究执行尚未接入，当前可编辑问题和选择示例。")}</span></div>
      {error && <p className={styles.error} role="alert">{error}</p>}
    </form>
  );
}

/** 时间弹窗：草稿选择与真实追问复用表单；后端负责将相对时长解析为采集截止范围。 */
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Button } from "../../../shared/ui/Button";
import type { TimeSelection } from "../contracts/session";
import styles from "../pages/ResearchSession.module.css";
interface Props { title: string; cutoff: string | null; initial: TimeSelection | null; confirmLabel: string; onConfirm: (selection: TimeSelection) => Promise<void>; onClose: () => void }
/** 父组件按追问编号和修订挂载；退出时恢复焦点，防止旧弹窗选择泄漏到新请求。 */
export function TimeRangeDialog({ title, cutoff, initial, confirmLabel, onConfirm, onClose }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [mode, setMode] = useState<"relative" | "absolute">(initial?.mode ?? "relative");
  const [amount, setAmount] = useState(initial?.mode === "relative" ? String(initial.amount) : "");
  const [unit, setUnit] = useState<"hour" | "day">(initial?.mode === "relative" ? initial.unit : "hour");
  const [start, setStart] = useState(initial?.mode === "absolute" ? initial.start.slice(0, 16) : "");
  const [end, setEnd] = useState(initial?.mode === "absolute" ? initial.endExclusive.slice(0, 16) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const dialog = ref.current, previous = document.activeElement;
    dialog?.showModal();
    return () => { dialog?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  /** 仅检查普通表单参数，不执行采集或链上核验，不编造缺失的数据截止。 */
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    let selection: TimeSelection;
    if (mode === "relative") {
      const value = Number(amount);
      if (!Number.isSafeInteger(value) || value <= 0) { setError("请输入正整数时长。"); return; }
      selection = { mode, amount: value, unit };
    } else {
      // 此表单使用北京时间，datetime-local 的无时区输入显式附上 +08:00。
      const a = `${start}:00+08:00`, b = `${end}:00+08:00`;
      const first = Date.parse(a), last = Date.parse(b);
      if (!Number.isFinite(first) || !Number.isFinite(last) || last <= first) { setError("请填写有效范围，结束时间须晚于开始时间。"); return; }
      if (cutoff && last > Date.parse(cutoff)) { setError("结束时间超出了当前已采集数据，请调整范围。"); return; }
      selection = { mode, start: a, endExclusive: b };
    }
    setBusy(true); setError(null);
    try { await onConfirm(selection); }
    catch { setError("未能保存时间选择，请稍后重试。"); }
    finally { setBusy(false); }
  }
  return <dialog ref={ref} className={styles.dialog} aria-labelledby={titleId} onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
    <form onSubmit={(event) => void submit(event)}><h2 id={titleId}>{title}</h2><p className={styles.dialogHint}>北京时间（UTC+8）。相对时长截至数据库已完成采集的数据时间。</p>
      <p className={styles.cutoff}>{cutoff ? `数据截至：${new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Shanghai" }).format(new Date(cutoff))}` : "接入研究服务后显示实际数据截止时间。"}</p>
      <fieldset disabled={busy} className={styles.timeFields}><legend>时间选择方式</legend>
        <label><input type="radio" name={titleId} checked={mode === "relative"} onChange={() => { setMode("relative"); setError(null); }} />最近一段时间</label>
        <label><input type="radio" name={titleId} checked={mode === "absolute"} onChange={() => { setMode("absolute"); setError(null); }} />指定起止时间</label>
        {mode === "relative" ? <div className={styles.timeRow}><label>时长<input autoFocus type="number" min="1" step="1" required value={amount} onChange={(event) => setAmount(event.target.value)} /></label><label>单位<select value={unit} onChange={(event) => setUnit(event.target.value === "day" ? "day" : "hour")}><option value="hour">小时</option><option value="day">天</option></select></label></div> : <div className={styles.absoluteFields}><label>开始时间<input type="datetime-local" required value={start} onChange={(event) => setStart(event.target.value)} /></label><label>结束时间（不包含）<input type="datetime-local" required value={end} onChange={(event) => setEnd(event.target.value)} /></label></div>}
      </fieldset>
      {error && <p className={styles.error} role="alert">{error}</p>}
      <div className={styles.dialogActions}><Button variant="secondary" disabled={busy} onClick={onClose}>暂不填写</Button><Button type="submit" loading={busy}>{confirmLabel}</Button></div>
    </form>
  </dialog>;
}

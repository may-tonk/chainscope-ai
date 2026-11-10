/** 会话输入：草稿编辑和真实发送分开，Enter 换行，不拦截中文输入法。 */
import { Clock3, Send, X } from "lucide-react";
import { useId } from "react";
import { Button } from "../../../shared/ui/Button";
import type { SessionDraft } from "../contracts/session";
import styles from "../pages/ResearchSession.module.css";
interface Props { draft: SessionDraft; onChange: (draft: SessionDraft) => void; enabled: boolean; busy: boolean; onSend: () => void; onTime: () => void }
/** 输入组件不决定哪些参数必填，具体任务由服务端 schema 判断。 */
export function SessionComposer({ draft, onChange, enabled, busy, onSend, onTime }: Props) {
  const id = useId();
  const timeLabel = draft.time?.mode === "relative" ? `最近 ${draft.time.amount} ${draft.time.unit === "hour" ? "小时" : "天"}` : draft.time ? "已选择起止时间" : "补充时间（按需）";
  return <form className={styles.composer} onSubmit={(event) => { event.preventDefault(); if (enabled && !busy && draft.question.trim()) onSend(); }}>
    <label htmlFor={id}>继续你的研究</label>
    {draft.objects.length > 0 && <ul className={styles.contexts} aria-label="草稿引用对象">{draft.objects.map((object) => <li key={`${object.chainId}:${object.address}`}><span title={object.address}>{object.symbol} · 链 {object.chainId}</span><button type="button" disabled={busy} aria-label={`移除 ${object.symbol} 对象`} onClick={() => onChange({ ...draft, objects: draft.objects.filter((item) => item.chainId !== object.chainId || item.address !== object.address) })}><X size={13} aria-hidden="true" /></button></li>)}</ul>}
    <textarea id={id} rows={3} value={draft.question} disabled={busy} placeholder="提出问题，或粘贴地址、合约、交易哈希…" onChange={(event) => onChange({ ...draft, question: event.target.value })} />
    <div className={styles.composerActions}><div><button type="button" className={styles.timeButton} disabled={busy} onClick={onTime}><Clock3 size={15} aria-hidden="true" />{timeLabel}</button>{draft.time && <button type="button" className={styles.textButton} disabled={busy} onClick={() => onChange({ ...draft, time: null })}>移除时间</button>}</div><Button type="submit" disabled={!enabled || !draft.question.trim()} loading={busy}>发送 <Send size={15} aria-hidden="true" /></Button></div>
    <p className={styles.inputHint}>{enabled ? "Enter 换行 · 时间按任务需要填写，单笔交易不强制填写。" : "研究服务尚未接入或当前任务尚不可发送，内容保留为草稿。"}</p>
  </form>;
}

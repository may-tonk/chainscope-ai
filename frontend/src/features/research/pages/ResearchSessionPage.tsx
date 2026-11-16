/** 单栏会话页：组合消息、执行事实、时间追问和独立报告入口，真实服务通过动作接口注入。 */
import { useState } from "react";
import { Link } from "react-router";
import { ArrowLeft, CircleStop, MessageSquare, Plus } from "lucide-react";
import type { SessionActions, SessionDraft, SessionLoadState, SessionStatus, TimeSelection } from "../contracts/session";
import { ResearchMessages } from "../components/ResearchMessages";
import { ResearchProgress } from "../components/ResearchProgress";
import { ResearchReportEntry } from "../components/ResearchReportEntry";
import { SessionComposer } from "../components/SessionComposer";
import { TimeRangeDialog } from "../components/TimeRangeDialog";
import styles from "./ResearchSession.module.css";
const labels: Record<SessionStatus, string> = { queued: "排队中", running: "研究中", needs_input: "待补充", cancelling: "正在停止", succeeded: "已完成", partial: "部分完成", failed: "执行失败", cancelled: "已取消" };
const EMPTY_DRAFT: SessionDraft = { question: "", objects: [], time: null };
interface Props { state: SessionLoadState; initialDraft?: SessionDraft; actions: SessionActions | null; disconnected?: boolean; onNew?: () => void }
/** 外层按账户与会话设置 key，使私人草稿和弹窗不跨账户或研究复用。 */
export function ResearchSessionPage({ state, initialDraft = EMPTY_DRAFT, actions, disconnected = false, onNew }: Props) {
  const [draft, setDraft] = useState(initialDraft);
  const [busy, setBusy] = useState<"send" | "clarify" | "cancel" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [timeOpen, setTimeOpen] = useState(false);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [cancelAcknowledged, setCancelAcknowledged] = useState<number | null>(null);
  const snapshot = state.snapshot;
  const clarification = snapshot?.status === "needs_input" ? snapshot.clarification : null;
  const clarificationKey = clarification ? `${clarification.id}:${clarification.requestRevision}` : null;
  const currentDialog = clarification && clarificationKey !== dismissed ? clarification : null;
  const canSend = !!actions && !disconnected && (state.status === "draft" || state.status === "ready") && snapshot?.status !== "cancelling";
  const canStop = !!actions && !!snapshot && cancelAcknowledged !== snapshot.requestRevision && ["queued", "running", "needs_input"].includes(snapshot.status) && !disconnected;
  /** 只有真实提交成功后清空草稿；失败显示安全提示，不自行插入假消息。 */
  async function send() {
    if (!actions || busy || !canSend || !draft.question.trim()) return;
    setBusy("send"); setError(null);
    try { await actions.send({ ...draft, question: draft.question.trim() }); setDraft(EMPTY_DRAFT); }
    catch { setError("未能发送问题，草稿已保留，请稍后重试。"); }
    finally { setBusy(null); }
  }
  /** 请求停止后仍等服务端终态，不将按钮点击当作后台已终止。 */
  async function cancel() {
    if (!actions || busy || !canStop) return;
    setBusy("cancel"); setError(null);
    try { await actions.cancel(); setCancelAcknowledged(snapshot?.requestRevision ?? null); }
    catch { setError("未能提交停止请求，请稍后重试。"); }
    finally { setBusy(null); }
  }
  /** 用户手动选择更新草稿；Agent 追问必须回传编号和原请求修订。 */
  async function chooseTime(selection: TimeSelection) {
    if (currentDialog) {
      if (!actions || snapshot?.requestRevision !== currentDialog.requestRevision) throw new Error("stale clarification");
      setBusy("clarify");
      try { await actions.clarify(currentDialog, selection); setDismissed(clarificationKey); }
      finally { setBusy(null); }
    } else { setDraft((previous) => ({ ...previous, time: selection })); setTimeOpen(false); }
  }
  return <div className={styles.page}>
    <div className={styles.breadcrumb}><Link to="/"><ArrowLeft size={14} aria-hidden="true" />研究空间</Link><span>/</span><span>AI 研究</span></div>
    <header className={styles.header}><div><span className={styles.kicker}><MessageSquare size={14} aria-hidden="true" /> AI RESEARCH</span><h1>{snapshot?.title ?? "新研究会话"}</h1><p>围绕问题追踪证据，在需要时补充条件，继续深入。</p></div><div className={styles.headerActions}><span className={styles.badge}>{snapshot ? labels[snapshot.status] : state.status === "loading" ? "读取中" : "草稿"}</span><button type="button" disabled={busy !== null} onClick={() => { setDraft(EMPTY_DRAFT); setTimeOpen(false); setError(null); onNew?.(); }}><Plus size={15} aria-hidden="true" />新建研究</button></div></header>
    <div className={styles.scope}><span>{snapshot?.focusLabel ? `重点范围：${snapshot.focusLabel}` : "时间与对象按具体研究任务确定"}</span><span>{snapshot?.dataCutoffTime ? `数据截至：${new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Shanghai" }).format(new Date(snapshot.dataCutoffTime))}` : "数据截至时间尚未接入"}</span></div>
    {state.status === "unavailable" && <p className={styles.notice} role="status">会话界面已就绪，研究服务尚未接入。你可以编辑问题、整理对象和时间，当前不会执行分析。</p>}
    {state.status === "loading" && <p className={styles.notice} role="status">正在读取研究会话…</p>}
    {state.status === "error" && <p className={styles.error} role="alert">无法读取研究会话，请稍后刷新重试。</p>}
    {disconnected && <p className={styles.error} role="alert">进度连接已断开，当前显示最后收到的结果，刷新后重新读取。断开连接不代表后台任务已停止。</p>}
    <ResearchProgress steps={snapshot?.steps ?? []} />
    <section className={styles.conversation} aria-label="研究对话"><ResearchMessages messages={snapshot?.messages ?? []} /></section>
    {clarification && <div className={styles.clarification}><div><strong>需要补充研究条件</strong><p>{clarification.question}</p></div><button type="button" disabled={!actions || busy !== null} onClick={() => setDismissed(null)}>填写时间</button></div>}
    {snapshot && ["queued", "running", "needs_input", "cancelling"].includes(snapshot.status) && <div className={styles.stopRow}><span>{cancelAcknowledged === snapshot.requestRevision ? "停止请求已提交，等待后台确认。" : "后台研究期间可以继续交流，也可以离开页面后重新查看。"}</span><button type="button" disabled={!canStop || busy !== null} onClick={() => void cancel()}><CircleStop size={15} aria-hidden="true" />{busy === "cancel" ? "正在请求停止…" : snapshot.status === "cancelling" ? "正在停止…" : cancelAcknowledged === snapshot.requestRevision ? "已请求停止" : "停止研究"}</button></div>}
    <ResearchReportEntry report={snapshot?.report ?? null} onOpen={actions?.openReport} />
    <SessionComposer draft={draft} onChange={setDraft} enabled={canSend} busy={busy !== null} onSend={() => void send()} onTime={() => setTimeOpen(true)} />
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {(timeOpen || (currentDialog && actions)) && <TimeRangeDialog key={clarificationKey ?? "draft-time"} title={currentDialog?.question ?? "补充研究时间"} cutoff={currentDialog?.dataCutoffTime ?? snapshot?.dataCutoffTime ?? null} initial={currentDialog ? null : draft.time} confirmLabel={currentDialog ? "提交时间" : "应用到草稿"} onConfirm={chooseTime} onClose={() => { setTimeOpen(false); if (currentDialog) setDismissed(clarificationKey); }} />}
  </div>;
}

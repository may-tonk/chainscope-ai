/** 会话展示及适配合同：身份、权限由后端处理，页面只携带资源编号与请求修订。 */
export type SessionStatus = "queued" | "running" | "needs_input" | "cancelling" | "succeeded" | "partial" | "failed" | "cancelled";
export type StepStatus = "queued" | "running" | "succeeded" | "failed" | "skipped";
export interface SessionMessage { id: string; role: "user" | "assistant"; text: string; evidenceIds: readonly string[] }
export interface ResearchStep { id: string; title: string; status: StepStatus; summary?: string; durationMs?: number }
/** 相对时间由后端按照现有采集截止解析，不生成“当前时间减三小时”。 */
export type TimeSelection = { mode: "relative"; amount: number; unit: "hour" | "day" } | { mode: "absolute"; start: string; endExclusive: string };
export interface TimeClarification { id: string; requestRevision: number; question: string; dataCutoffTime: string }
export interface ReportReference { id: string; version: string; title: string }
export interface SessionSnapshot {
  id: string; requestRevision: number; sequence: number; title: string; status: SessionStatus;
  dataCutoffTime: string | null; focusLabel: string | null;
  messages: readonly SessionMessage[]; steps: readonly ResearchStep[];
  clarification: TimeClarification | null; report: ReportReference | null;
}
interface EventBase { eventId: string; sessionId: string; requestRevision: number; sequence: number }
/** sequence 在同一会话内单调增加；事件可回放，重复消息按稳定编号更新。 */
export type SessionEvent = EventBase & (
  | { type: "snapshot"; snapshot: SessionSnapshot }
  | { type: "message"; message: SessionMessage }
  | { type: "step"; step: ResearchStep }
  | { type: "status"; status: SessionStatus }
  | { type: "clarification"; clarification: TimeClarification | null }
  | { type: "report"; report: ReportReference }
);
export type SessionLoadState = { status: "unavailable" | "loading" | "error" | "draft"; snapshot: null } | { status: "ready"; snapshot: SessionSnapshot };
export interface SessionDraft { question: string; objects: readonly { chainId: number; address: string; symbol: string }[]; time: TimeSelection | null }
/** 真实服务的扩展接口，当前没有模拟实现；订阅需要支持从指定序号回放以避免加载间隙丢事件。 */
export interface ResearchSessionService {
  load(id: string, signal: AbortSignal): Promise<SessionSnapshot>;
  subscribe(id: string, afterSequence: number, receive: (event: SessionEvent) => void, onDisconnect: () => void): () => void;
  send(input: { sessionId: string | null; requestRevision: number | null; text: string; objects: SessionDraft["objects"]; time: TimeSelection | null }): Promise<SessionSnapshot>;
  clarify(input: { sessionId: string; clarificationId: string; requestRevision: number; time: TimeSelection }): Promise<void>;
  cancel(input: { sessionId: string; requestRevision: number }): Promise<void>;
}
export interface SessionActions {
  send: (draft: SessionDraft) => Promise<void>;
  clarify: (request: TimeClarification, time: TimeSelection) => Promise<void>;
  cancel: () => Promise<void>;
  openReport?: (report: ReportReference) => void;
}

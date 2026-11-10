/** 会话纯状态归并：不让跨会话、旧修订和乱序事件覆盖新结果。 */
import type { SessionEvent, SessionLoadState, SessionSnapshot } from "../contracts/session";
export type SessionAction = { type: "loading" } | { type: "error" } | { type: "snapshot"; snapshot: SessionSnapshot } | { type: "event"; event: SessionEvent };
/** 使用稳定编号更新流式消息与步骤，重复事件不制造重复项目。 */
function upsert<T extends { id: string }>(items: readonly T[], item: T): readonly T[] {
  return items.some((existing) => existing.id === item.id) ? items.map((existing) => existing.id === item.id ? item : existing) : [...items, item];
}
/** 一个控制器只维护一个会话，快照的修订和序号必须向前。 */
function acceptSnapshot(state: SessionLoadState, snapshot: SessionSnapshot): SessionLoadState {
  const current = state.snapshot;
  if (current && (current.id !== snapshot.id || snapshot.requestRevision < current.requestRevision || snapshot.sequence <= current.sequence)) return state;
  return { status: "ready", snapshot };
}
/** 根据事件类型更新已知快照；新修订必须通过完整快照建立。 */
export function sessionReducer(state: SessionLoadState, action: SessionAction): SessionLoadState {
  if (action.type === "loading") return state.snapshot ? state : { status: "loading", snapshot: null };
  if (action.type === "error") return state.snapshot ? state : { status: "error", snapshot: null };
  if (action.type === "snapshot") return acceptSnapshot(state, action.snapshot);
  const event = action.event;
  if (event.type === "snapshot") {
    if (event.snapshot.id !== event.sessionId || event.snapshot.sequence !== event.sequence || event.snapshot.requestRevision !== event.requestRevision) return state;
    return acceptSnapshot(state, event.snapshot);
  }
  const current = state.snapshot;
  if (!current || current.id !== event.sessionId || current.requestRevision !== event.requestRevision || event.sequence <= current.sequence) return state;
  const next = { ...current, sequence: event.sequence };
  switch (event.type) {
    case "message": return { status: "ready", snapshot: { ...next, messages: upsert(current.messages, event.message) } };
    case "step": return { status: "ready", snapshot: { ...next, steps: upsert(current.steps, event.step) } };
    case "status": return { status: "ready", snapshot: { ...next, status: event.status, clarification: event.status === "needs_input" ? current.clarification : null } };
    case "clarification":
      if (event.clarification && event.clarification.requestRevision !== current.requestRevision) return state;
      return { status: "ready", snapshot: { ...next, clarification: event.clarification } };
    case "report": return { status: "ready", snapshot: { ...next, report: event.report } };
  }
}

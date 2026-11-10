/** 异步加载和事件订阅：卸载时释放连接，状态由真实快照推进，不以动画模拟。 */
import { useEffect, useReducer, useState } from "react";
import type { ResearchSessionService, SessionLoadState, SessionSnapshot } from "../contracts/session";
import { sessionReducer } from "../state/sessionState";
/** 服务应保持稳定引用；外层通过账户和会话 key 隔离本地状态。 */
export function useResearchSession(id: string | null, service: ResearchSessionService | null) {
  const initial: SessionLoadState = { status: service ? id ? "loading" : "draft" : "unavailable", snapshot: null };
  const [state, dispatch] = useReducer(sessionReducer, initial);
  const [disconnected, setDisconnected] = useState(false);
  useEffect(() => {
    if (!service || !id) return;
    const controller = new AbortController();
    let active = true;
    let loaded = false;
    let unsubscribe: (() => void) | undefined;
    void service.load(id, controller.signal).then((snapshot) => {
      if (!active) return;
      if (snapshot.id !== id) { dispatch({ type: "error" }); return; }
      dispatch({ type: "snapshot", snapshot });
      loaded = true;
      unsubscribe = service.subscribe(id, snapshot.sequence, (event) => {
        if (active && event.sessionId === id) { setDisconnected(false); dispatch({ type: "event", event }); }
      }, () => { if (active) setDisconnected(true); });
    }).catch(() => {
      if (!active || controller.signal.aborted) return;
      // 快照已经读取但订阅建立失败时，保留结果并明确标记断线，避免界面假装持续更新。
      if (loaded) setDisconnected(true);
      else dispatch({ type: "error" });
    });
    return () => { active = false; controller.abort(); unsubscribe?.(); };
  }, [id, service]);
  /** 发送动作成功后只接受服务快照，不自行设置完成或取消。 */
  function accept(snapshot: SessionSnapshot) { dispatch({ type: "snapshot", snapshot }); }
  return { state, disconnected, accept };
}

/** 持久聊天控制器：URL 关联会话，PG 历史恢复，取消与旧请求回调按代次隔离。 */
import { useEffect, useRef, useState } from "react";
import { asApiError } from "../../shared/api/client";
import type { ChatPageController } from "../research/pages/ChatPage";
import * as api from "./api";

type ViewTurn = ChatPageController["turns"][number];
interface Active { key: string; controller: AbortController; turnId?: string; revision?: number; }
interface SentRequest { conversationId: string; revision: number; terminal: boolean; }

/** 数据库消息按 turn_id 组成页面投影，失败和停止的半截正文完整保留。 */
function project(messages: api.StoredMessage[]): ViewTurn[] {
  const groups = new Map<string, { user?: api.StoredMessage; assistant?: api.StoredMessage }>();
  for (const message of messages) {
    const group = groups.get(message.turn_id) ?? {};
    group[message.role] = message;
    groups.set(message.turn_id, group);
  }
  return Array.from(groups, ([id, { user, assistant }]) => ({
    id, question: user?.content ?? "", answer: assistant?.content ?? "",
    status: assistant?.status === "completed" ? "complete" : assistant?.status === "running" ? "streaming" : assistant?.status === "queued" ? "waiting" : assistant?.status ?? "waiting",
    dispatchPending: false,
    error: assistant?.status === "failed" ? "该轮回复未完成，可以重新提问。" : null,
  }));
}

/** 回调由路由提供；首次创建后 replace URL，匹配当前 ID 时保持正在进行的流。 */
export function usePersistentChat(routeId: string | undefined, routeKey: string, created: (id: string) => void, changed: () => void) {
  const [turns, setTurns] = useState<readonly ViewTurn[]>([]);
  const [conversation, setConversation] = useState<api.Conversation | null>(null);
  const [issue, setIssue] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const data = useRef<readonly ViewTurn[]>([]);
  const current = useRef<api.Conversation | null>(null);
  const active = useRef<Active | null>(null);
  const epoch = useRef(0);
  const mounted = useRef(true);
  const creating = useRef<Promise<api.Conversation> | null>(null);
  const callbacks = useRef({ created, changed });
  const sent = useRef(new Map<string, SentRequest>());
  const waitingRemote = turns.some((row) => row.status === "waiting" || row.status === "streaming");
  useEffect(() => { callbacks.current = { created, changed }; }, [created, changed]);

  /** 所有异步发布都经过 mounted/代次校验，账户切换后不保留私人文本。 */
  function publish(rows: readonly ViewTurn[]) { data.current = rows; if (mounted.current) setTurns(rows); }
  /** 仅更新指定轮次显示，其他轮次保持其独立状态。 */
  function update(key: string, patch: Partial<ViewTurn>) { publish(data.current.map((row) => row.id === key ? { ...row, ...patch } : row)); }

  /** 逐页读取历史，界面不复制服务端分页数量。 */
  async function refresh(id: string, generation: number, onlyIdle = false) {
    let page = await api.getSnapshot(id);
    const messages = [...page.messages];
    while (page.has_more) {
      if (!mounted.current || epoch.current !== generation) return;
      page = await api.getSnapshot(id, page.next_sequence);
      messages.push(...page.messages);
    }
    if (!mounted.current || epoch.current !== generation || (onlyIdle && active.current)) return;
    current.current = page.conversation;
    setConversation(page.conversation);
    publish(project(messages));
    return page;
  }

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; epoch.current += 1; active.current?.controller.abort(); active.current = null; };
  }, []);

  useEffect(() => {
    if (routeId && current.current?.conversation_id === routeId) return;
    const generation = ++epoch.current;
    active.current?.controller.abort();
    active.current = null;
    current.current = null;
    creating.current = null;
    sent.current.clear();
    // 同步复位属于路由切换，避免新会话短暂展示上一会话数据。
    publish([]);
    setConversation(null);
    setIssue(null);
    setLoading(Boolean(routeId));
    if (routeId) void refresh(routeId, generation).catch((error) => {
      if (mounted.current && epoch.current === generation) setIssue(asApiError(error).message);
    }).finally(() => { if (mounted.current && epoch.current === generation) setLoading(false); });
    // 路由标识就是切换依据；闭包使用 ref 中的最新值。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeId, routeKey]);

  useEffect(() => {
    if (!routeId || !waitingRemote || active.current) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const generation = epoch.current;
    /** 恢复页短暂读到未收尾轮次时，根据后端提示刷新，最终显示停止状态。 */
    const poll = async () => {
      if (active.current || cancelled) return;
      try {
        const page = await refresh(routeId, generation, true);
        if (!cancelled && page && data.current.some((row) => row.status === "waiting" || row.status === "streaming")) timer = setTimeout(() => void poll(), page.poll_after_ms);
      } catch (error) { if (!cancelled && epoch.current === generation) setIssue(asApiError(error).message); }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeId, waitingRemote]);

  /** 显式停止先作废回调，再 abort；已知服务端轮次时也发送停止命令。 */
  function stop() {
    const previous = active.current;
    active.current = null;
    previous?.controller.abort();
    const old = previous ? data.current.find((row) => row.id === previous.key) : undefined;
    if (previous && old && (old.status === "waiting" || old.status === "streaming")) update(previous.key, { status: "stopped" });
    const row = current.current;
    const last = data.current.at(-1);
    const turnId = previous?.turnId ?? (last && (last.status === "waiting" || last.status === "streaming") ? last.id : undefined);
    const revision = previous?.revision ?? row?.revision;
    if (row && turnId && revision) void api.stopTurn(row.conversation_id, turnId, revision).catch(() => undefined);
  }

  /** 每次发送只传新问题。网络不确定时保留幂等键供重放，终态重试创建新轮次。 */
  function send(question: string, replay?: { key: string; input?: SentRequest }) {
    if (!question.trim() || loading) return;
    stop();
    const generation = epoch.current;
    const key = replay?.key ?? crypto.randomUUID();
    const controller = new AbortController();
    const item: Active = { key, controller };
    active.current = item;
    setIssue(null);
    if (replay) update(key, { answer: "", status: "waiting", dispatchPending: false, error: null });
    else publish([...data.current, { id: key, question, answer: "", status: "waiting", dispatchPending: false, error: null }]);
    void (async () => {
      try {
        if (!current.current) {
          const promise = creating.current ?? api.createConversation(question);
          creating.current = promise;
          const row = await promise;
          if (creating.current === promise) creating.current = null;
          if (!mounted.current || epoch.current !== generation) return;
          current.current = row;
          setConversation(row);
          callbacks.current.created(row.conversation_id);
        }
        if (active.current !== item || controller.signal.aborted) return;
        const row = current.current;
        const snapshot = await api.getSnapshot(row.conversation_id);
        if (active.current !== item || controller.signal.aborted) return;
        current.current = snapshot.conversation;
        setConversation(snapshot.conversation);
        const input = replay?.input ?? { conversationId: row.conversation_id, revision: snapshot.conversation.revision, terminal: false };
        sent.current.set(key, input);
        await api.streamTurn(input.conversationId, input.revision, key, question, controller.signal, (event) => {
          if (!mounted.current || epoch.current !== generation || active.current !== item) return;
          item.turnId = event.turn_id;
          item.revision = event.request_revision;
          if (event.request_revision && current.current) {
            current.current = { ...current.current, revision: event.request_revision };
            setConversation(current.current);
          }
          const old = data.current.find((turn) => turn.id === key);
          if (!old) return;
          const payload = event.payload;
          if (["turn.completed", "turn.failed", "turn.interrupted"].includes(payload.type)) input.terminal = true;
          if (payload.type === "message.delta") update(key, { answer: old.answer + payload.text, status: "streaming" });
          if (payload.type === "turn.completed") update(key, { answer: payload.text, status: "complete" });
          if (payload.type === "turn.interrupted") update(key, { status: "stopped" });
          if (payload.type === "turn.failed") update(key, { status: "failed", error: payload.message });
          if (payload.type === "decision.ready") update(key, { dispatchPending: payload.dispatch_status === "not_connected" });
        });
      } catch (error) {
        if (active.current === item) creating.current = null;
        // 修订冲突发生在本轮接收前，重试应刷新版本；不确定的网络错误继续复用原幂等键。
        if (asApiError(error).code === "conversation_changed") sent.current.delete(key);
        if (mounted.current && epoch.current === generation && active.current === item && !controller.signal.aborted) update(key, { status: "failed", error: asApiError(error).message });
      } finally {
        if (active.current === item) { active.current = null; callbacks.current.changed(); }
      }
    })();
  }

  /** 失败已经是持久终态，重新回答保留旧失败记录并建立新轮次。 */
  function retry() {
    const last = data.current.at(-1);
    if (!last || !["failed", "stopped"].includes(last.status)) return;
    const input = sent.current.get(last.id);
    if (last.status === "failed" && (!input || !input.terminal)) send(last.question, { key: last.id, input });
    else send(last.question);
  }
  /** 手动重新读取数据库状态，方便网络失败后确认已保存的实际结果。 */
  function reload() {
    const generation = epoch.current;
    if (current.current && !active.current) void refresh(current.current.conversation_id, generation).catch((error) => { if (mounted.current && generation === epoch.current) setIssue(asApiError(error).message); });
  }
  return { turns, conversation, issue, loading, send, stop, retry, reload, streaming: turns.some((row) => row.status === "waiting" || row.status === "streaming") };
}

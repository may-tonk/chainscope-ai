/** 持久聊天路由：历史切换、URL 恢复与删除入口，复用研究聊天阅读布局。 */
import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { asApiError } from "../../shared/api/client";
import { useAuth } from "../auth/session/AuthContext";
import { ChatPage } from "../research/pages/ChatPage";
import { readOwnedDraft } from "../research/state/sessionState";
import * as api from "./api";
import { usePersistentChat } from "./usePersistentChat";
import styles from "./PersistentChatRoute.module.css";

/** 账户变化时重建全部引用，避免私人消息跨账户进入组件状态。 */
export function ChatRoute() {
  const { state } = useAuth();
  if (state.status !== "authenticated") return null;
  return <OwnedChat key={state.account.user_id} owner={state.account.user_id} />;
}

/** 会话 URL 改变时保持同一个控制器，首次创建替换 URL 不会打断正文生成。 */
function OwnedChat({ owner }: { owner: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const { conversationId } = useParams();
  const [history, setHistory] = useState<api.Conversation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const [version, setVersion] = useState(0);
  const created = useCallback((id: string) => { void navigate(`/chat/${id}`, { replace: true }); }, [navigate]);
  const changed = useCallback(() => setVersion((old) => old + 1), []);
  const chat = usePersistentChat(conversationId, location.key, created, changed);

  useEffect(() => {
    let alive = true;
    void api.listConversations().then((rows) => { if (alive) { setHistory(rows); setMore(rows.length > 0); } }).catch((reason) => { if (alive) setError(asApiError(reason).message); });
    return () => { alive = false; };
  }, [version]);

  /** 更多会话使用服务端提供的记录作为游标，稳定去重，保留当前选择。 */
  async function loadMore() {
    try {
      const rows = await api.listConversations(history.at(-1));
      setHistory((old) => [...old, ...rows.filter((row) => !old.some((item) => item.conversation_id === row.conversation_id))]);
      setMore(rows.length > 0);
    } catch (reason) { setError(asApiError(reason).message); }
  }

  /** 删除有实际确认入口；先停止当前生成，再按服务端修订删除自己的会话。 */
  async function remove() {
    if (!chat.conversation || !window.confirm("删除这段对话及其消息？")) return;
    chat.stop();
    try {
      const latest = await api.getSnapshot(chat.conversation.conversation_id);
      await api.deleteConversation(latest.conversation);
      changed();
      void navigate("/research/new");
    } catch (reason) { setError(asApiError(reason).message); }
  }

  return <div className={styles.page}>
    <div className={styles.toolbar}>
      <button type="button" onClick={() => void navigate("/research/new")}>新建对话</button>
      <label>历史对话<select aria-label="历史对话" value={conversationId ?? ""} onChange={(event) => { if (event.target.value) void navigate(`/chat/${event.target.value}`); }}>
        <option value="">选择对话</option>
        {chat.conversation && !history.some((row) => row.conversation_id === chat.conversation?.conversation_id) && <option value={chat.conversation.conversation_id}>{chat.conversation.title}</option>}
        {history.map((row) => <option key={row.conversation_id} value={row.conversation_id}>{row.title}</option>)}
      </select></label>
      {more && <button type="button" onClick={() => void loadMore()}>更多</button>}
      {chat.conversation && <button type="button" onClick={() => void remove()}>删除对话</button>}
      {chat.conversation && <button type="button" onClick={chat.reload}>刷新记录</button>}
    </div>
    {(error || chat.issue) && <p role="alert" className={styles.notice}>{error ?? chat.issue}</p>}
    {chat.loading && <p role="status" className={styles.notice}>正在读取历史对话…</p>}
    <ChatPage key={conversationId ?? location.key} controller={chat} initialDraft={readOwnedDraft(location.state, owner)} />
  </div>;
}

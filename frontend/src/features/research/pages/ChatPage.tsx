/** 已登录聊天页：消息独立滚动、输入框常驻底部，仅维护当前页面的对话。 */
import { ArrowDown, Sparkles } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import { useLocation } from "react-router";
import { useAuth } from "../../auth/session/AuthContext";
import { ResearchMessages } from "../components/ResearchMessages";
import { SessionComposer } from "../components/SessionComposer";
import { TimeRangeDialog } from "../components/TimeRangeDialog";
import type { SessionDraft, SessionMessage } from "../contracts/session";
import type { ChatTurn } from "../contracts/session";
import { useChat } from "../hooks/useChat";
import { readOwnedDraft } from "../state/sessionState";
import styles from "./ChatPage.module.css";

const EMPTY: SessionDraft = { question: "", objects: [], time: null };

/** 页面只依赖显示状态，持久控制器和旧协议验证控制器均可复用同一布局。 */
export interface ChatPageController {
  turns: readonly Pick<ChatTurn, "id" | "question" | "answer" | "status" | "dispatchPending" | "error">[];
  streaming: boolean;
  loading?: boolean;
  send(question: string): void;
  stop(): void;
  retry(): void;
}

/** 切换账户或新建同路径对话时重建状态，不继承私人文本。 */
export function ChatRoute() {
  const { state } = useAuth();
  const location = useLocation();
  if (state.status !== "authenticated") return null;
  return (
    <ChatPage
      key={`${state.account.user_id}:${location.key}`}
      initialDraft={readOwnedDraft(location.state, state.account.user_id)}
    />
  );
}

/** 用户选择的对象/时间作为明确上下文发送，不伪造数据截止或工具证据。 */
function questionText(draft: SessionDraft): string {
  const details = draft.objects.map(
    (item) =>
      `研究对象：${item.symbol}，链 ${item.chainId}，合约 ${item.address}`
  );
  if (draft.time?.mode === "relative")
    details.push(
      `用户选择的研究范围：最近 ${draft.time.amount} ${
        draft.time.unit === "hour" ? "小时" : "天"
      }，实际截止由数据服务确定。`
    );
  else if (draft.time)
    details.push(
      `用户选择的研究范围：${draft.time.start} 至 ${draft.time.endExclusive}（不含结束时刻）。`
    );
  return [draft.question.trim(), ...details].join("\n");
}

/** 请求状态来自 SSE；停止或离页时取消当前连接，不创建持久历史会话。 */
export function ChatPage({
  initialDraft = EMPTY,
  controller,
}: {
  initialDraft?: SessionDraft;
  controller?: ChatPageController;
}) {
  const local = useChat();
  const chat = controller ?? local;
  const [draft, setDraft] = useState(initialDraft);
  const [timeOpen, setTimeOpen] = useState(false);
  const viewport = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const messages: SessionMessage[] = chat.turns.flatMap((turn) => [
    {
      id: `${turn.id}:user`,
      role: "user",
      text: turn.question,
      evidenceIds: [],
    },
    {
      id: `${turn.id}:assistant`,
      role: "assistant",
      text: turn.answer,
      evidenceIds: [],
      ...(turn.status === "complete" ? {} : { delivery: turn.status }),
    },
  ]);
  const last = chat.turns.at(-1);
  // 分片到达时仅在用户本来位于底部的情况下跟随，向上阅读不会被强制拉回。
  useLayoutEffect(() => {
    if (following.current && viewport.current) {
      viewport.current.scrollTop = viewport.current.scrollHeight;
    }
  }, [chat.turns]);

  // 窗口变窄、文字重新换行或软键盘改变可用高度时，也要维护原来的跟随意图。
  // 同时观察消息列和视口，只有 following 为 true 才调整位置；卸载时释放观察器。
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (following.current) element.scrollTop = element.scrollHeight;
    });
    observer.observe(element);
    if (element.firstElementChild) observer.observe(element.firstElementChild);
    return () => observer.disconnect();
  }, []);

  /** 接近底部就继续跟随；否则显示跳转按钮，让用户自己决定何时回到最新内容。 */
  function handleScroll() {
    const element = viewport.current;
    if (!element) return;
    following.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < 80;
    setShowLatest(!following.current);
  }

  /** 主动发送、重试或点击跳转时恢复跟随；不使用持续平滑动画干扰流式阅读。 */
  function jumpToLatest() {
    following.current = true;
    setShowLatest(false);
    if (viewport.current)
      viewport.current.scrollTop = viewport.current.scrollHeight;
  }

  /** 新问题立即进入发送状态；失败保留问题并提供同轮重试。 */
  function send() {
    if (!draft.question.trim()) return;
    jumpToLatest();
    chat.send(questionText(draft));
    setDraft(EMPTY);
  }
  return (
    <section
      className={`${styles.page} ${messages.length ? "" : styles.empty}`}
      aria-label="AI 研究对话"
    >
      <div
        className={styles.viewport}
        ref={viewport}
        onScroll={handleScroll}
        role="region"
        aria-label="聊天消息区"
        tabIndex={0}
      >
        <div className={styles.timeline}>
          {messages.length ? (
            <ResearchMessages messages={messages} variant="chat" />
          ) : (
            <div className={styles.welcome}>
              <span className={styles.welcomeIcon}>
                <Sparkles size={25} aria-hidden="true" />
              </span>
              <h1>今天想研究什么？</h1>
              <p>从一个链上问题开始，一起梳理思路。</p>
            </div>
          )}
          {last?.dispatchPending && (
            <p className={styles.notice}>
              本次链上分析暂不可执行，你可以继续讨论或补充研究条件。
            </p>
          )}
        </div>
      </div>
      <div className={styles.dock}>
        {showLatest && messages.length > 0 && (
          <button
            className={styles.latest}
            type="button"
            onClick={jumpToLatest}
            aria-label="查看最新回复"
          >
            <ArrowDown size={18} aria-hidden="true" />
          </button>
        )}
        {(last?.status === "failed" || (controller && last?.status === "stopped")) && (
          <div className={styles.error}>
            {last?.error && <p role="alert">{last.error}</p>}
            <button
              type="button"
              onClick={() => {
                jumpToLatest();
                chat.retry();
              }}
            >
              {controller ? "重新回答此问题" : "重试本轮回复"}
            </button>
          </div>
        )}
        <SessionComposer
          variant="chat"
          draft={draft}
          onChange={setDraft}
          enabled={!controller?.loading}
          busy={false}
          streaming={chat.streaming}
          onStop={chat.stop}
          onSend={send}
          onTime={() => setTimeOpen(true)}
          hint={
            chat.streaming
              ? "Enter 发送新问题并停止上一条回复 · Shift+Enter 换行"
              : "Enter 发送 · Shift+Enter 换行"
          }
        />
        <div className={styles.meta}>
          <span role="status">
            {chat.streaming
              ? "正在生成回复…"
              : last?.status === "complete"
              ? "回复完成"
              : last?.status === "stopped"
              ? "已停止接收本轮回复"
              : ""}
          </span>
          <span>{controller ? "对话保存到你的账户 · 离页停止生成" : "对话仅保留在当前页面"}</span>
        </div>
      </div>
      {timeOpen && (
        <TimeRangeDialog
          title="补充研究时间"
          cutoff={null}
          initial={draft.time}
          confirmLabel="应用到问题"
          onConfirm={async (time) => {
            setDraft((previous) => ({ ...previous, time }));
            setTimeOpen(false);
          }}
          onClose={() => setTimeOpen(false)}
        />
      )}
    </section>
  );
}

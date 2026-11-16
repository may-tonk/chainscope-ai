/** 共用会话输入：聊天模式支持 Enter 发送及输入框自适应高度，研究模式保持原行为。 */
import { ArrowUp, Clock3, Send, Square, X } from "lucide-react";
import { useId, useLayoutEffect, useRef } from "react";
import { Button } from "../../../shared/ui/Button";
import type { SessionDraft } from "../contracts/session";
import styles from "../pages/ResearchSession.module.css";
interface Props {
  draft: SessionDraft;
  onChange: (draft: SessionDraft) => void;
  enabled: boolean;
  busy: boolean;
  onSend: () => void;
  onTime: () => void;
  hint?: string;
  variant?: "research" | "chat";
  streaming?: boolean;
  onStop?: () => void;
}
/** 输入组件不决定哪些参数必填，具体任务由服务端 schema 判断。 */
export function SessionComposer({
  draft,
  onChange,
  enabled,
  busy,
  onSend,
  onTime,
  hint,
  variant = "research",
  streaming = false,
  onStop,
}: Props) {
  const id = useId();
  const textarea = useRef<HTMLTextAreaElement>(null);
  const isChat = variant === "chat";
  // 内容缩短时先恢复自动高度；最大高度由 CSS 和这里共同约束，多余文本在框内滚动。
  useLayoutEffect(() => {
    if (!isChat || !textarea.current) return;
    textarea.current.style.height = "auto";
    textarea.current.style.height = `${Math.min(
      textarea.current.scrollHeight,
      180
    )}px`;
  }, [draft.question, isChat]);
  const timeLabel =
    draft.time?.mode === "relative"
      ? `最近 ${draft.time.amount} ${
          draft.time.unit === "hour" ? "小时" : "天"
        }`
      : draft.time
      ? "已选择起止时间"
      : "补充时间（按需）";
  return (
    <form
      className={
        isChat ? `${styles.composer} ${styles.chatComposer}` : styles.composer
      }
      onSubmit={(event) => {
        event.preventDefault();
        if (enabled && !busy && draft.question.trim()) onSend();
      }}
    >
      <label htmlFor={id} className={isChat ? "visually-hidden" : undefined}>
        继续你的研究
      </label>
      {draft.objects.length > 0 && (
        <ul className={styles.contexts} aria-label="草稿引用对象">
          {draft.objects.map((object) => (
            <li key={`${object.chainId}:${object.address}`}>
              <span title={object.address}>
                {object.symbol} · 链 {object.chainId}
              </span>
              <button
                type="button"
                disabled={busy}
                aria-label={`移除 ${object.symbol} 对象`}
                onClick={() =>
                  onChange({
                    ...draft,
                    objects: draft.objects.filter(
                      (item) =>
                        item.chainId !== object.chainId ||
                        item.address !== object.address
                    ),
                  })
                }
              >
                <X size={13} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <textarea
        id={id}
        ref={textarea}
        rows={isChat ? 1 : 3}
        value={draft.question}
        disabled={busy}
        placeholder="提出问题，或粘贴地址、合约、交易哈希…"
        onChange={(event) =>
          onChange({ ...draft, question: event.target.value })
        }
        onKeyDown={(event) => {
          // 中文输入法确认候选词也是 Enter，不能误发送；Shift+Enter 始终保留换行。
          if (
            !isChat ||
            event.key !== "Enter" ||
            event.shiftKey ||
            event.nativeEvent.isComposing ||
            event.keyCode === 229
          )
            return;
          event.preventDefault();
          event.currentTarget.form?.requestSubmit();
        }}
      />
      <div className={styles.composerActions}>
        <div>
          <button
            type="button"
            className={styles.timeButton}
            disabled={busy}
            onClick={onTime}
          >
            <Clock3 size={15} aria-hidden="true" />
            {timeLabel}
          </button>
          {draft.time && (
            <button
              type="button"
              className={styles.textButton}
              disabled={busy}
              onClick={() => onChange({ ...draft, time: null })}
            >
              移除时间
            </button>
          )}
        </div>
        <div className={styles.composerSubmit}>
          {isChat && streaming && onStop && (
            <button
              type="button"
              className={styles.chatStopButton}
              onClick={onStop}
              aria-label="停止回复"
              title="停止回复"
            >
              <Square size={15} fill="currentColor" aria-hidden="true" />
            </button>
          )}
          {(!isChat || !streaming || draft.question.trim()) && (
            <Button
              type="submit"
              className={isChat ? styles.chatSendButton : undefined}
              disabled={!enabled || !draft.question.trim()}
              loading={busy}
              title={isChat ? "发送" : undefined}
            >
              {isChat ? (
                <>
                  <span className="visually-hidden">发送</span>
                  <ArrowUp size={19} aria-hidden="true" />
                </>
              ) : (
                <>
                  发送 <Send size={15} aria-hidden="true" />
                </>
              )}
            </Button>
          )}
        </div>
      </div>
      <p className={styles.inputHint}>
        {hint ??
          (enabled
            ? "Enter 换行 · 时间按任务需要填写，单笔交易不强制填写。"
            : "研究服务尚未接入或当前任务尚不可发送，内容保留为草稿。")}
      </p>
    </form>
  );
}

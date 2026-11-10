/** 消息采用 React 文本渲染，不执行模型 HTML；证据显示已授权引用编号。 */
import { MessageSquare, Sparkles } from "lucide-react";
import type { SessionMessage } from "../contracts/session";
import styles from "../pages/ResearchSession.module.css";
/** 空消息展示真实空态，不伪造用户发送或 Agent 回答。 */
export function ResearchMessages({ messages }: { messages: readonly SessionMessage[] }) {
  if (!messages.length) return <div className={styles.emptyConversation}><span className={styles.emptyIcon}><MessageSquare size={25} aria-hidden="true" /></span><h2>从一个具体问题开始</h2><p>描述你想了解的变化，或粘贴地址、合约和交易哈希。需要补充信息时，会在这里向你追问。</p></div>;
  return <ol className={styles.messages} aria-label="研究消息">{messages.map((message) => <li key={message.id} className={message.role === "user" ? styles.userMessage : styles.assistantMessage}>
    <div className={styles.messageRole}>{message.role === "user" ? "你" : <><Sparkles size={14} aria-hidden="true" /> ChainScope</>}</div>
    <p>{message.text}</p>{message.evidenceIds.length > 0 && <div className={styles.evidence}>证据引用：{message.evidenceIds.join("、")}</div>}
  </li>)}</ol>;
}

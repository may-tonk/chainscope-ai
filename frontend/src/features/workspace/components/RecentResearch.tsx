/** 最近研究：服务未接入和真实空列表分别展示，由调用方注入异步状态。 */
import { ArrowUpRight, History, LoaderCircle } from "lucide-react";
import { formatDate } from "../../../shared/format/date";
import type { RecentResearchState, ResearchStatus } from "../types/research";
import styles from "./RecentResearch.module.css";
const labels: Record<ResearchStatus, string> = { queued: "排队中", running: "研究中", needs_input: "待补充", partial: "部分完成", succeeded: "已完成", failed: "执行失败", cancelled: "已取消" };
interface Props { state: RecentResearchState; onOpen?: (id: string) => void }
/** 不自行发请求或拼接未实现的路由；有真实打开回调时才启用列表操作。 */
export function RecentResearch({ state, onOpen }: Props) {
  return <section className={styles.section} aria-labelledby="recent-research-title" aria-busy={state.status === "loading"}>
    <div className={styles.heading}><h2 id="recent-research-title">最近研究</h2><span>保留问题，延续研究</span></div>
    {state.status === "ready" && state.items.length > 0 ? <ul className={styles.list}>{state.items.map((item) => <li key={item.id}>
      <button type="button" className={styles.row} disabled={!onOpen} onClick={() => onOpen?.(item.id)}><span className={styles.rowTitle}>{item.title}</span><span className={styles.status}>{labels[item.status]}</span><time dateTime={item.updatedAt}>{formatDate(item.updatedAt)}</time><ArrowUpRight size={16} aria-hidden="true" /></button>
    </li>)}</ul> : <div className={styles.empty} role={state.status === "error" ? "alert" : "status"}>
      <span className={styles.icon}>{state.status === "loading" ? <LoaderCircle size={21} aria-hidden="true" /> : <History size={21} aria-hidden="true" />}</span>
      <div><p>{state.status === "unavailable" ? "研究记录尚未接入" : state.status === "loading" ? "正在读取研究记录…" : state.status === "error" ? "暂时无法读取研究记录" : "还没有研究记录"}</p>
      <span>{state.status === "unavailable" ? "接入研究服务后，可在这里返回会话、查看进度并继续追问。" : state.status === "error" ? state.message : state.status === "loading" ? "请稍候。" : "发起研究后，你的会话会显示在这里。"}</span></div>
    </div>}
  </section>;
}

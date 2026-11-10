/** 研究入口页：组合问题输入、示例和最近研究，账户操作沿用现有认证外壳。 */
import { ArrowUpRight, Sparkles, Telescope, type LucideIcon } from "lucide-react";
import { useRef, useState } from "react";
import { RecentResearch } from "../components/RecentResearch";
import { ResearchComposer } from "../components/ResearchComposer";
import { ResearchExamples } from "../components/ResearchExamples";
import { researchExamples } from "../data/researchExamples";
import type { RecentResearchState, ResearchDraft, ResearchExample, ResearchExecution } from "../types/research";
import styles from "./HomePage.module.css";
export interface FeaturePreview { id: string; label: string; description: string; icon: LucideIcon }
interface Props { upcoming: readonly FeaturePreview[]; execution?: ResearchExecution; recentResearch?: RecentResearchState; onOpenResearch?: (id: string) => void; submitLabel?: string; executionHint?: string }
const UNAVAILABLE_EXECUTION: ResearchExecution = { status: "unavailable" };
const UNAVAILABLE_HISTORY: RecentResearchState = { status: "unavailable" };
/** 默认明确标记未接入；未来注入真实服务时不重写页面结构，也不在模块全局保存用户草稿。 */
export function HomePage({ upcoming, execution = UNAVAILABLE_EXECUTION, recentResearch = UNAVAILABLE_HISTORY, onOpenResearch, submitLabel, executionHint }: Props) {
  const [draft, setDraft] = useState<ResearchDraft>({ question: "", contexts: [] });
  const inputRef = useRef<HTMLTextAreaElement>(null);
  /** 示例只填入可编辑草稿，并聚焦输入框；不自动执行，也不编造哈希。 */
  function selectExample(example: ResearchExample) {
    setDraft({ question: example.question, contexts: example.context ? [example.context] : [] });
    inputRef.current?.focus();
  }
  return <div className={styles.page}>
    <header className={styles.header}>
      <span className={styles.eyebrow}><Sparkles size={14} aria-hidden="true" /> CHAIN RESEARCH WORKSPACE</span>
      <h1>研究空间</h1>
      <p>从交易、地址到代币，用自然语言提出问题，让数据与证据连接起来。</p>
    </header>
    <ResearchComposer draft={draft} onChange={setDraft} execution={execution} inputRef={inputRef} submitLabel={submitLabel} executionHint={executionHint} />
    <ResearchExamples examples={researchExamples} onSelect={selectExample} />
    <div className={styles.divider} />
    <RecentResearch state={recentResearch} onOpen={onOpenResearch} />
    {upcoming.length > 0 && <section className={styles.explore} aria-labelledby="workspace-features-title">
      <div className={styles.sectionHeading}><h2 id="workspace-features-title">你的研究，逐步积累</h2><span>以下功能尚未开放</span></div>
      <ul className={styles.featureGrid}>{upcoming.map(({ id, label, description, icon: Icon }) => <li key={id} className={styles.featureCard}>
        <Icon size={19} aria-hidden="true" /><div><h3>{label}</h3><p>{description}</p></div><ArrowUpRight size={15} aria-hidden="true" />
      </li>)}</ul>
    </section>}
    <footer className={styles.footer}><Telescope size={15} aria-hidden="true" /><span>把每次研究，积累成自己的研究方法。</span></footer>
  </div>;
}

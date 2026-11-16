/** 示例仅填充草稿，不自动提交；配置与组件分离以便扩展研究方向。 */
import { ArrowUpRight, ChartNoAxesCombined, Search, Wallet } from "lucide-react";
import type { ResearchExample } from "../types/research";
import styles from "./ResearchExamples.module.css";
interface Props { examples: readonly ResearchExample[]; onSelect: (example: ResearchExample) => void }
const icons = [ChartNoAxesCombined, Wallet, Search];
/** 按配置渲染问题建议；没有配置时不显示此区块。 */
export function ResearchExamples({ examples, onSelect }: Props) {
  if (!examples.length) return null;
  return <section aria-labelledby="research-examples-title" className={styles.section}>
    <div className={styles.heading}><h2 id="research-examples-title">不确定从哪里开始？</h2><span>选择一个问题，继续编辑</span></div>
    <div className={styles.grid}>{examples.map((example, index) => {
      const Icon = icons[index % icons.length] ?? Search;
      return <button type="button" className={styles.card} key={example.id} onClick={() => onSelect(example)}>
        <span className={styles.cardTop}><span className={styles.category}><Icon size={15} aria-hidden="true" />{example.category}</span><ArrowUpRight size={16} aria-hidden="true" /></span>
        <span className={styles.title}>{example.title}</span><span className={styles.description}>{example.description}</span>
      </button>;
    })}</div>
  </section>;
}

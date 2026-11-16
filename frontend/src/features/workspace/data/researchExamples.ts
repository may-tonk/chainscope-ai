/** 可替换的问题示例，只填充草稿，不携带虚构分析结果或自动执行任务。 */
import type { ResearchContext, ResearchExample } from "../types/research";
const BEM: ResearchContext = { chainId: 56, address: "0x5ce033b2bfca3af30b3e8c8457deaf776a8b695a", symbol: "BEM" };
export const researchExamples: readonly ResearchExample[] = [
  { id: "activity", category: "活动变化", title: "BEM 的活动发生了什么变化？", description: "从一个问题开始，逐步明确时间和研究范围。", question: "帮我分析 BEM 最近的链上活动变化，区分普通转账与零地址发出事件。", context: BEM },
  { id: "holders", category: "持仓分布", title: "持仓是否变得更加集中？", description: "结合持仓快照和比较范围，观察筹码分布。", question: "我想了解 BEM 的持仓分布，以及主要地址的持仓集中度变化。", context: BEM },
  { id: "transaction", category: "交易解析", title: "这笔交易里发生了什么？", description: "粘贴真实交易哈希，围绕具体交易展开研究。", question: "请帮我分析这笔 BSC 交易：" },
];

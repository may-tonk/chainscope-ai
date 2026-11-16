/** 研究入口合同：草稿不携带用户身份或权限；可执行能力由真实服务适配器注入。 */
export interface ResearchContext { chainId: number; address: string; symbol: string }
export interface ResearchDraft { question: string; contexts: readonly ResearchContext[] }
export type ResearchExecution = { status: "unavailable" } | { status: "available"; submit: (draft: ResearchDraft) => Promise<void> };
export type ResearchStatus = "queued" | "running" | "needs_input" | "partial" | "succeeded" | "failed" | "cancelled";
export interface ResearchSummary { id: string; title: string; status: ResearchStatus; updatedAt: string }
/** 未接入、加载、失败和真实空列表分别表达，避免用空数组掩盖服务未实现。 */
export type RecentResearchState = { status: "unavailable" } | { status: "loading" } | { status: "error"; message: string } | { status: "ready"; items: readonly ResearchSummary[] };
export interface ResearchExample { id: string; category: string; title: string; description: string; question: string; context?: ResearchContext }

/** 会话测试专用数据：不进入生产页面或冒充真实研究结果。 */
import type { SessionSnapshot } from "../src/features/research/contracts/session";
/** 构造可覆盖字段的服务快照，测试只关注协议与交互，不验证虚构链上结论。 */
export function snapshot(overrides: Partial<SessionSnapshot> = {}): SessionSnapshot {
  return { id: "session-a", requestRevision: 1, sequence: 10, title: "测试研究", status: "running", dataCutoffTime: "2026-11-07T06:00:00Z", focusLabel: "测试时间范围", messages: [], steps: [], clarification: null, report: null, ...overrides };
}

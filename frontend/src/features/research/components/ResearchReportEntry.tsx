/** 报告独立阅读，对话页仅放入口与版本，不建立固定完整报告画布。 */
import { ArrowUpRight, FileText } from "lucide-react";
import type { ReportReference } from "../contracts/session";
import styles from "../pages/ResearchSession.module.css";
/** 未接入阅读器时禁用入口，不导航到不存在的页面。 */
export function ResearchReportEntry({ report, onOpen }: { report: ReportReference | null; onOpen?: (report: ReportReference) => void }) {
  return <section className={styles.report} aria-label="研究报告入口"><FileText size={20} aria-hidden="true" /><div><h2>{report ? report.title : "研究报告"}</h2><p>{report ? `版本 ${report.version}${onOpen ? " · 独立阅读完整图表与证据" : " · 报告阅读页面尚未接入"}` : "报告生成后，可在独立页面阅读图表、统计口径与证据。"}</p></div><button type="button" disabled={!report || !onOpen} onClick={() => { if (report) onOpen?.(report); }}>查看报告 <ArrowUpRight size={15} aria-hidden="true" /></button></section>;
}

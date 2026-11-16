/**
 * 认证外壳：登录、注册、忘记密码三类页面共用（参照参考图的登录页，配色改为全站统一的深绿色）。
 *
 *   ┌───────────────────────┬──────────────────┐
 *   │ ChainScope             │                  │
 *   │ 让每一步研究，          │   表单（<Outlet />）│
 *   │ 有据可查。              │                  │
 *   │ 几条功能介绍            │                  │
 *   └───────────────────────┴──────────────────┘
 *
 * 左侧只放口号和功能介绍，不放图表和示例数据（2026-10-25 用户确认）：
 * 登录前展示真实数据需要另做公开接口，不在第一版范围；放假数据又会误导。
 * 窄屏上左侧整块隐藏，只在表单上方显示 Logo。
 */
import { FileText, ShieldCheck, Waypoints, type LucideIcon } from "lucide-react";
import { Outlet } from "react-router";

import styles from "./AuthLayout.module.css";

/** 左侧的功能介绍。描述的是 ChainScope 要做的事，下面注明“陆续上线”，不暗示已全部可用。 */
const HIGHLIGHTS: { icon: LucideIcon; title: string; text: string }[] = [
  {
    icon: Waypoints,
    title: "资金流向",
    text: "按地址和时间范围梳理转入、转出与净流向。",
  },
  {
    icon: ShieldCheck,
    title: "证据可追溯",
    text: "每个结论都标明数据来源；未经核验的内容明确标注“待核验”。",
  },
  {
    icon: FileText,
    title: "研究报告",
    text: "把每次研究整理成可以反复查看、持续补充的报告。",
  },
];

/** 认证外壳组件：左侧品牌介绍，右侧放当前页面（登录、注册等）的表单。 */
export function AuthLayout() {
  return (
    <div className={styles.shell}>
      <section className={styles.brandPanel} aria-label="ChainScope 介绍">
        <p className={styles.logo}>ChainScope</p>
        <div className={styles.intro}>
          <p className={styles.eyebrow}>CHAIN RESEARCH</p>
          {/* 大口号用宋体类字体，与参考图一致；其余文字用黑体类。 */}
          <h1 className={styles.hero}>
            让每一步研究，
            <br />
            有据可查。
          </h1>
          <p className={styles.subtitle}>从资金流向到发行机制，整理数据，追溯证据。</p>
        </div>
        <ul className={styles.highlights}>
          {HIGHLIGHTS.map(({ icon: Icon, title, text }) => (
            <li key={title} className={styles.highlight}>
              <span className={styles.highlightIcon}>
                <Icon size={18} aria-hidden="true" />
              </span>
              <div>
                <p className={styles.highlightTitle}>{title}</p>
                <p className={styles.highlightText}>{text}</p>
              </div>
            </li>
          ))}
        </ul>
        <p className={styles.note}>研究功能正在建设中，将陆续上线。</p>
      </section>

      <main className={styles.formPanel}>
        <div className={styles.formArea}>
          {/* 窄屏上左侧介绍隐藏，这里补一个 Logo。 */}
          <p className={styles.mobileLogo}>ChainScope</p>
          <Outlet />
        </div>
      </main>
    </div>
  );
}

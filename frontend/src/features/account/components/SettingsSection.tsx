/**
 * 设置页的一个分区卡片：左边是标题和说明，右边是内容（表单或信息）。
 *
 * 所有设置分区都用它，排版只写在这一处：
 * - 宽屏：左右两栏，左栏说明“这是什么、会有什么后果”，右栏放操作，表单不会被拉得过宽；
 * - 窄屏：上下排列。
 * danger 为 true 时是“危险区”样式（红色边框和标题），用于注销账户这类不可撤销的操作。
 *
 * 以后增加新的设置项（例如修改昵称、通知设置），只需要再放一个 SettingsSection。
 */
import { useId, type ReactNode } from "react";

import styles from "./SettingsSection.module.css";

interface SettingsSectionProps {
  title: string;
  /** 标题下方的说明文字。 */
  description: ReactNode;
  /** 危险区样式。 */
  danger?: boolean;
  /** 右栏内容：表单、信息列表等。 */
  children: ReactNode;
}

/** 设置分区卡片组件。 */
export function SettingsSection({ title, description, danger = false, children }: SettingsSectionProps) {
  const titleId = useId();
  return (
    <section
      className={danger ? `${styles.section} ${styles.danger}` : styles.section}
      aria-labelledby={titleId}
    >
      <div className={styles.intro}>
        <h2 id={titleId} className={styles.title}>
          {title}
        </h2>
        <div className={styles.description}>{description}</div>
      </div>
      <div className={styles.content}>{children}</div>
    </section>
  );
}

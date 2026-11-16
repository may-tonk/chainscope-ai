/**
 * 提示条：在表单上方显示一段带颜色和图标的提示。
 *
 * 四种类型：error（错误，红）、warning（警告，黄）、success（成功，绿）、info（说明，蓝）。
 * 出错时可以附带请求编号（requestId）：用户反馈问题时报出这个编号，
 * 就能在后端审计记录里找到对应的那一次请求。
 *
 * role="alert" 会让读屏软件在提示出现时立即读出来；普通说明用 role="status"，不打断用户。
 */
import { CircleAlert, CircleCheck, Info, TriangleAlert, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import styles from "./Alert.module.css";

type AlertVariant = "error" | "warning" | "success" | "info";

const ICONS: Record<AlertVariant, LucideIcon> = {
  error: CircleAlert,
  warning: TriangleAlert,
  success: CircleCheck,
  info: Info,
};

interface AlertProps {
  variant: AlertVariant;
  children: ReactNode;
  /** 后端返回的请求编号，显示在提示下方的小字里。 */
  requestId?: string | null;
}

/** 提示条组件：按 variant 选图标和颜色，有请求编号时显示在下方。 */
export function Alert({ variant, children, requestId }: AlertProps) {
  const Icon = ICONS[variant];
  const urgent = variant === "error" || variant === "warning";
  return (
    <div className={`${styles.alert} ${styles[variant]}`} role={urgent ? "alert" : "status"}>
      <Icon className={styles.icon} size={18} aria-hidden="true" />
      <div className={styles.body}>
        <div>{children}</div>
        {requestId && <p className={styles.requestId}>请求编号：{requestId}</p>}
      </div>
    </div>
  );
}

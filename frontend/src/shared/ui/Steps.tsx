/**
 * 步骤条：显示多步流程的进度，例如注册的“① 验证邮箱 → ② 输入验证码 → ③ 设置密码”。
 *
 * - 已完成的步骤：绿色圆圈里打勾；
 * - 当前步骤：深绿色实心圆圈，文字加粗，并标记 aria-current="step"，读屏软件会读出“当前步骤”；
 * - 还没到的步骤：灰色空心圆圈。
 *
 * 步骤条只用来显示进度，不能点击跳步：后面的步骤依赖前一步从后端拿到的凭证。
 */
import { Check } from "lucide-react";

import styles from "./Steps.module.css";

interface StepsProps {
  /** 每一步的名称，按顺序排列。 */
  labels: string[];
  /** 当前是第几步，从 0 开始。 */
  current: number;
  /** 给读屏软件的说明，例如“注册进度”。 */
  ariaLabel: string;
}

/** 步骤条组件：按 current 把每一步画成“已完成 / 当前 / 未开始”三种样式之一。 */
export function Steps({ labels, current, ariaLabel }: StepsProps) {
  return (
    <ol className={styles.steps} aria-label={ariaLabel}>
      {labels.map((label, index) => {
        const done = index < current;
        const active = index === current;
        const className = [styles.step, done ? styles.done : "", active ? styles.active : ""]
          .filter(Boolean)
          .join(" ");
        return (
          <li key={label} className={className} aria-current={active ? "step" : undefined}>
            <span className={styles.marker} aria-hidden="true">
              {done ? <Check size={14} strokeWidth={3} /> : index + 1}
            </span>
            <span className={styles.label}>
              {label}
              {done && <span className="visually-hidden">（已完成）</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

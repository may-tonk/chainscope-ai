/**
 * 通用按钮。全站的按钮都用它，保证外观和行为一致。
 *
 * 三种样式：
 * - primary：深绿色实心，每个页面最主要的操作（登录、下一步）；
 * - secondary：白底带边框，次要操作（取消、返回）；
 * - danger：红色，危险且不可撤销的操作（注销账户）。
 *
 * loading 为 true 时显示转圈图标并禁止点击，防止用户连点导致重复提交
 * （例如重复发送验证码邮件，触发限速）。
 */
import { LoaderCircle } from "lucide-react";
import type { ButtonHTMLAttributes } from "react";

import styles from "./Button.module.css";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "danger";
  /** 正在提交：显示转圈并禁用按钮。 */
  loading?: boolean;
  /** 占满一整行（表单里的主按钮）。 */
  block?: boolean;
}

/** 按钮组件：其余属性（onClick、aria-* 等）原样交给原生 <button>。 */
export function Button({
  variant = "primary",
  loading = false,
  block = false,
  disabled,
  className,
  children,
  // 默认 type="button"：HTML 里按钮放在表单中默认会提交表单，容易误触发；需要提交时显式写 type="submit"。
  type = "button",
  ...rest
}: ButtonProps) {
  const classes = [styles.button, styles[variant], block ? styles.block : "", className ?? ""]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      {...rest}
      type={type}
      className={classes}
      disabled={disabled || loading}
      // aria-busy：告诉读屏软件这个按钮正在处理中。
      aria-busy={loading || undefined}
    >
      {loading && <LoaderCircle className={styles.spinner} size={16} aria-hidden="true" />}
      {children}
    </button>
  );
}

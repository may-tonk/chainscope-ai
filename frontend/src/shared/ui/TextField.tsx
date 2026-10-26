/**
 * 带标题的输入框：上面是标题（label），中间是输入框，下面是提示或错误信息。
 *
 * 标题和输入框通过 id 关联：点击标题会聚焦输入框，读屏软件也能读出这个输入框叫什么。
 * 有错误时输入框标红，并用 aria-invalid / aria-describedby 把错误告诉读屏软件。
 *
 * 密码框在 PasswordField.tsx，它复用这里的外观，再加上“显示/隐藏密码”按钮。
 */
import { useId, type InputHTMLAttributes, type ReactNode, type Ref } from "react";

import styles from "./TextField.module.css";

export interface TextFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  /** 输入框下方的灰色说明，例如“8～128 个字符”。 */
  hint?: string;
  /** 输入框下方的红色错误信息；有值时输入框标红。 */
  error?: string;
  /** 输入框右侧的附加内容（密码框的眼睛按钮）。 */
  trailing?: ReactNode;
  /**
   * 指向内部 <input> 的引用，调用方可以用它把光标移到输入框（例如验证码输错后）。
   * React 19 起，函数组件可以直接把 ref 当普通参数接收。
   */
  ref?: Ref<HTMLInputElement>;
}

/** 输入框组件：其余属性（value、onChange、autoComplete 等）原样交给原生 <input>。 */
export function TextField({
  label,
  hint,
  error,
  trailing,
  id,
  className,
  ref,
  ...rest
}: TextFieldProps) {
  // useId 生成页面内唯一的 id，同一页面放多个输入框也不会冲突。
  const autoId = useId();
  const inputId = id ?? autoId;
  const messageId = `${inputId}-message`;
  const message = error ?? hint;

  return (
    <div className={[styles.field, className ?? ""].filter(Boolean).join(" ")}>
      <label className={styles.label} htmlFor={inputId}>
        {label}
      </label>
      <div className={[styles.control, error ? styles.invalid : ""].filter(Boolean).join(" ")}>
        <input
          {...rest}
          ref={ref}
          id={inputId}
          className={styles.input}
          aria-invalid={error ? true : undefined}
          aria-describedby={message ? messageId : undefined}
        />
        {trailing}
      </div>
      {message && (
        <p id={messageId} className={error ? styles.error : styles.hint}>
          {message}
        </p>
      )}
    </div>
  );
}

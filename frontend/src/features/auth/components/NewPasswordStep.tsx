/**
 * 多步流程的最后一步：设置新密码。注册使用；忘记密码（设置新密码）也会复用。
 *
 * 细节（2026-10-25 用户确认）：
 * - 不加“确认密码”框：有“显示密码”按钮可以看清输入内容，忘了还能重置；
 * - 密码框下方实时显示已输入的字符数和规则“8～128 个字符”，不用等提交才知道够不够长；
 * - autoComplete="new-password"：浏览器和密码管理器会提议生成并保存一个强密码。
 *
 * 为了让密码管理器把“邮箱 + 新密码”一起保存成一条记录，表单里放一个看不见的、
 * 只读的邮箱字段（autoComplete="username"）。这是浏览器厂商推荐的做法。
 *
 * 错误显示：
 * - 密码不符合规则（前端检查，或后端 password_too_short / password_too_long / password_common）：
 *   显示在密码框下方；
 * - 流程已失效（注册为 registration_failed，重置密码为 password_reset_failed）：
 *   上方提示条 + “重新开始”按钮，回到第 1 步；
 * - 其他错误：上方提示条，附请求编号。
 */
import { useState, type FormEvent } from "react";

import { asApiError, type ApiError } from "../../../shared/api/client";
import { Alert } from "../../../shared/ui/Alert";
import { Button } from "../../../shared/ui/Button";
import { PasswordField } from "../../../shared/ui/PasswordField";
import { useCountdown } from "../../../shared/ui/useCountdown";
import {
  PASSWORD_ERROR_CODES,
  PASSWORD_MAX_LENGTH,
  newPasswordHint,
  validateNewPassword,
} from "../../../shared/validation/password";
import styles from "./AuthForm.module.css";

interface NewPasswordStepProps {
  /** 这个密码属于哪个邮箱：显示在说明里，也供密码管理器保存。 */
  email: string;
  /** 密码框的名称：注册时是“密码”，重置时是“新密码”；说明文字“正在为…设置…”也用它。 */
  passwordLabel: string;
  /** 提交按钮的文字，例如“完成注册”。 */
  submitLabel: string;
  /** 后端表示“整个流程已失效、要从头开始”的错误编号，例如 registration_failed。 */
  restartCode: string;
  /** 提交通过前端检查的密码（原样，不去空格）；失败时抛出 ApiError。 */
  onSubmit: (password: string) => Promise<void>;
  /** 流程失效时，用户点“重新开始”回到第 1 步。 */
  onRestart: () => void;
}

/** 设置密码表单组件：一个新密码输入框（含实时长度提示）和一个提交按钮。 */
export function NewPasswordStep({
  email,
  passwordLabel,
  submitLabel,
  restartCode,
  onSubmit,
  onRestart,
}: NewPasswordStepProps) {
  const [password, setPassword] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const countdown = useCountdown();
  const waiting = countdown.remaining > 0;
  const needsRestart = error?.code === restartCode;

  /** 提交：先检查长度，再交给 onSubmit；按错误类型把提示放到对应位置。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const problem = validateNewPassword(password);
    setFieldError(problem);
    setError(null);
    if (problem) {
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(password);
      // 成功后父组件会跳转到登录页，本组件卸载。
    } catch (caught) {
      const apiError = asApiError(caught);
      if (PASSWORD_ERROR_CODES.has(apiError.code)) {
        setFieldError(apiError.message);
      } else {
        setError(apiError);
        if (apiError.code === "rate_limited") {
          countdown.start(apiError.retryAfter);
        }
      }
      setSubmitting(false);
    }
  }

  // 实时提示：没输入时说明规则；输入后显示已输入多少个字符（共用 shared/validation 的规则）。
  const hint = newPasswordHint(password);

  return (
    <form className={styles.form} onSubmit={(event) => void handleSubmit(event)} noValidate>
      <div className={styles.stepIntro}>
        <p className={styles.stepText}>
          正在为 <span className={styles.email}>{email}</span> 设置{passwordLabel}
        </p>
      </div>

      {error && (
        <Alert variant="error" requestId={error.requestId}>
          {error.message}
          {waiting && `（${countdown.text}后可以再试）`}
        </Alert>
      )}

      {/* 给密码管理器看的邮箱：屏幕上不显示、键盘 Tab 跳不到、读屏软件也不读。 */}
      <input
        type="email"
        name="username"
        autoComplete="username"
        value={email}
        readOnly
        tabIndex={-1}
        aria-hidden="true"
        className="visually-hidden"
      />

      <PasswordField
        label={passwordLabel}
        name="new-password"
        autoComplete="new-password"
        placeholder="设置登录密码"
        value={password}
        onChange={(event) => {
          setPassword(event.target.value);
          setFieldError(null);
        }}
        hint={hint}
        error={fieldError ?? undefined}
        // 前端只粗略限制输入框长度，防止粘贴超长内容；准确的 128 字符检查在上面和后端。
        maxLength={PASSWORD_MAX_LENGTH * 4}
        autoFocus
        disabled={needsRestart}
      />

      {needsRestart ? (
        <Button block onClick={onRestart}>
          重新开始
        </Button>
      ) : (
        <Button type="submit" block loading={submitting} disabled={waiting}>
          {waiting ? `请 ${countdown.text}后再试` : submitLabel}
        </Button>
      )}
    </form>
  );
}

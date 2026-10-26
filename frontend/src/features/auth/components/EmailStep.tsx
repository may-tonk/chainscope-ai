/**
 * 多步流程的第 1 步：填写邮箱，获取验证码。注册使用；忘记密码（实施第 4 步）也会复用。
 *
 * 组件只负责表单本身：输入、前端格式检查、显示错误、防止重复提交。
 * 真正“向后端申请验证码”由调用方通过 onSubmit 传进来，所以注册和忘记密码可以共用这个组件。
 *
 * 错误显示的位置：
 * - 邮箱格式不对（前端检查或后端 invalid_email）：显示在邮箱输入框下方；
 * - 发得太频繁（rate_limited）：上方提示条 + 按钮上的倒计时；
 * - 其他错误（网络、服务繁忙）：上方提示条，附请求编号。
 */
import { useState, type FormEvent } from "react";

import { asApiError, type ApiError } from "../../../shared/api/client";
import { Alert } from "../../../shared/ui/Alert";
import { Button } from "../../../shared/ui/Button";
import { TextField } from "../../../shared/ui/TextField";
import { useCountdown } from "../../../shared/ui/useCountdown";
import { EMAIL_MAX_LENGTH, validateEmail } from "../../../shared/validation/email";
import styles from "./AuthForm.module.css";

interface EmailStepProps {
  /** 输入框的初始内容（例如从第 2 步点“修改邮箱”回来时，保留原来填的邮箱）。 */
  initialEmail: string;
  /** 输入框下方的说明文字。 */
  hint: string;
  /** 提交按钮的文字，例如“获取验证码”。 */
  submitLabel: string;
  /** 提交通过前端检查的邮箱（已去掉首尾空格）；失败时抛出 ApiError。 */
  onSubmit: (email: string) => Promise<void>;
}

/** 邮箱表单组件：一个邮箱输入框和一个提交按钮。 */
export function EmailStep({ initialEmail, hint, submitLabel, onSubmit }: EmailStepProps) {
  const [email, setEmail] = useState(initialEmail);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const countdown = useCountdown();
  const waiting = countdown.remaining > 0;

  /** 提交：先做前端格式检查，通过后交给 onSubmit；按错误类型把提示放到对应位置。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formatError = validateEmail(email);
    setFieldError(formatError);
    setError(null);
    if (formatError) {
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(email.trim());
      // 成功后父组件会切换到下一步，本组件随之卸载，不需要在这里做别的。
    } catch (caught) {
      const apiError = asApiError(caught);
      if (apiError.code === "invalid_email") {
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

  return (
    <form className={styles.form} onSubmit={(event) => void handleSubmit(event)} noValidate>
      {error && (
        <Alert variant="error" requestId={error.requestId}>
          {error.message}
          {waiting && `（${countdown.text}后可以再试）`}
        </Alert>
      )}
      <TextField
        label="邮箱"
        type="email"
        name="email"
        autoComplete="email"
        inputMode="email"
        placeholder="you@example.com"
        value={email}
        onChange={(event) => {
          setEmail(event.target.value);
          // 用户开始修改时，旧的格式错误提示就不再适用，先收起来。
          setFieldError(null);
        }}
        hint={hint}
        error={fieldError ?? undefined}
        maxLength={EMAIL_MAX_LENGTH}
        // 进入这一步时光标直接放在输入框里，用户可以马上输入。
        autoFocus
      />
      <Button type="submit" block loading={submitting} disabled={waiting}>
        {waiting ? `请 ${countdown.text}后再试` : submitLabel}
      </Button>
    </form>
  );
}

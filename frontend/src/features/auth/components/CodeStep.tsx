/**
 * 多步流程的第 2 步：输入邮件里的 6 位验证码。注册使用；忘记密码也会复用。
 *
 * 页面上的内容：
 * - 说明“请查看 xxx 的收件箱”，以及后端返回的提示（含“请检查垃圾邮件”）；
 * - 一个输入框（2026-10-25 用户确认：一个框输入 6 位，而不是 6 个小格子），
 *   粘贴“123 456”这类带空格的内容会自动整理成 123456；
 *   autoComplete="one-time-code" 让手机键盘能直接建议邮件或短信里的验证码；
 * - “重新发送验证码”按钮：验证码刚发出时先倒数 60 秒（与后端“同一邮箱 60 秒 1 封”一致）；
 * - “修改邮箱”：回到第 1 步，邮箱保留。
 *
 * 错误显示：
 * - 验证码错误、过期或已作废（verification_failed）：显示在输入框下方，并清空输入框；
 * - 被限速（rate_limited）：上方提示条 + 对应按钮的倒计时；
 * - 其他错误：上方提示条，附请求编号。
 */
import { useRef, useState, type FormEvent } from "react";

import { asApiError, type ApiError } from "../../../shared/api/client";
import { Alert } from "../../../shared/ui/Alert";
import { Button } from "../../../shared/ui/Button";
import { TextField } from "../../../shared/ui/TextField";
import { useCountdown } from "../../../shared/ui/useCountdown";
import { CODE_LENGTH, cleanCode, validateCode } from "../../../shared/validation/code";
import styles from "./AuthForm.module.css";

/** 发出验证码后，多少秒内不能重新发送（与后端规则 register_resend / reset_resend 一致）。 */
const RESEND_SECONDS = 60;

interface CodeStepProps {
  /** 验证码发往的邮箱，显示在说明里。 */
  email: string;
  /** 后端申请验证码时返回的提示，例如“如果这个邮箱可以注册，验证码已经发出……”。 */
  notice: string;
  /** 提交 6 位验证码；失败时抛出 ApiError。 */
  onVerify: (code: string) => Promise<void>;
  /** 重新发送验证码；失败时抛出 ApiError。成功后调用方会更新 notice。 */
  onResend: () => Promise<void>;
  /** 回到第 1 步修改邮箱。 */
  onChangeEmail: () => void;
}

/** 验证码表单组件：验证码输入框、确认按钮、重新发送和修改邮箱。 */
export function CodeStep({ email, notice, onVerify, onResend, onChangeEmail }: CodeStepProps) {
  const [code, setCode] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [resent, setResent] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [resending, setResending] = useState(false);
  // 两个独立的倒计时：提交验证码被限速时用 verifyWait；重新发送用 resendWait（一进来就先倒数 60 秒）。
  const verifyWait = useCountdown();
  const resendWait = useCountdown(RESEND_SECONDS);
  const inputRef = useRef<HTMLInputElement>(null);

  /** 提交验证码：先检查是否填满 6 位，再交给 onVerify。 */
  async function handleVerify(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const problem = validateCode(code);
    setFieldError(problem);
    setError(null);
    if (problem) {
      return;
    }
    setVerifying(true);
    try {
      await onVerify(code);
      // 成功后父组件切换到第 3 步，本组件卸载。
    } catch (caught) {
      const apiError = asApiError(caught);
      if (apiError.code === "verification_failed") {
        // 输错的验证码留着没有用，清空并把光标放回输入框，方便直接重输。
        setFieldError(apiError.message);
        setCode("");
        inputRef.current?.focus();
      } else {
        setError(apiError);
        if (apiError.code === "rate_limited") {
          verifyWait.start(apiError.retryAfter);
        }
      }
      setVerifying(false);
    }
  }

  /** 重新发送验证码：成功后清空输入框、重新倒数 60 秒；被限速时按后端给的秒数倒数。 */
  async function handleResend() {
    setResending(true);
    setError(null);
    setFieldError(null);
    try {
      await onResend();
      setCode("");
      setResent(true);
      resendWait.start(RESEND_SECONDS);
      inputRef.current?.focus();
    } catch (caught) {
      const apiError = asApiError(caught);
      setError(apiError);
      if (apiError.code === "rate_limited") {
        resendWait.start(apiError.retryAfter);
      }
    } finally {
      setResending(false);
    }
  }

  const verifyBlocked = verifyWait.remaining > 0;

  return (
    <form className={styles.form} onSubmit={(event) => void handleVerify(event)} noValidate>
      <div className={styles.stepIntro}>
        {/* 只说“请查看收件箱”，不说“验证码已发送”或“已发送邮件”：
            注册时邮箱已注册，后端发的是提醒邮件而不是验证码；重置密码时邮箱没有账户，后端根本不发信。
            这句话在所有情况下都成立，也不会暴露邮箱是否注册过（2026-10-26 调整）。 */}
        <p className={styles.stepText}>
          请查看 <span className={styles.email}>{email}</span> 的收件箱
        </p>
      </div>

      {/* 后端的提示措辞是“如果这个邮箱可以注册……”，故意不说邮箱是否已注册，这里原样显示。 */}
      <Alert variant={resent ? "success" : "info"}>
        {resent ? `已重新发送。${notice}` : notice}
      </Alert>

      {error && (
        <Alert variant="error" requestId={error.requestId}>
          {error.message}
        </Alert>
      )}

      <TextField
        ref={inputRef}
        label="验证码"
        name="code"
        // 用 text 而不是 number：number 类型的输入框会出现上下箭头，还会吞掉开头的 0。
        type="text"
        inputMode="numeric"
        autoComplete="one-time-code"
        placeholder={`${CODE_LENGTH} 位数字`}
        value={code}
        onChange={(event) => {
          setCode(cleanCode(event.target.value));
          setFieldError(null);
        }}
        hint="验证码 5 分钟内有效；输错 5 次后需要重新获取"
        error={fieldError ?? undefined}
        autoFocus
      />

      <Button type="submit" block loading={verifying} disabled={verifyBlocked}>
        {verifyBlocked ? `请 ${verifyWait.text}后再试` : "下一步"}
      </Button>

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.linkButton}
          onClick={() => void handleResend()}
          disabled={resending || resendWait.remaining > 0}
        >
          {resending
            ? "正在发送…"
            : resendWait.remaining > 0
              ? `${resendWait.text}后可重新发送`
              : "重新发送验证码"}
        </button>
        <button type="button" className={styles.linkButton} onClick={onChangeEmail}>
          修改邮箱
        </button>
      </div>
    </form>
  );
}

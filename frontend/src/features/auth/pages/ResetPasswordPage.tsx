/**
 * 忘记密码页（地址 /reset-password，只限未登录）。
 *
 *   ① 验证邮箱：填邮箱 → 后端在回复之后发送验证码（邮箱没有账户时不发信，但回复完全一样）
 *   ② 输入验证码：提交验证码 → 后端返回重置凭证（10 分钟有效、只能用一次）
 *   ③ 设置新密码：提交新密码和凭证 → 后端只更新密码，所有设备退出登录，账户和内容都保留
 *   完成后跳到登录页，邮箱已填好，提示“密码已重置……请用新密码登录”。
 *
 * 从登录页点“忘记密码？”过来时，会带上登录页已经填的邮箱，不用再输一遍。
 *
 * 三步流程本身由通用组件 EmailCodeFlow 负责（注册页也用它），
 * 这个页面只提供“配置”：调用重置密码的三个接口、显示相应的文字、完成后去哪。
 */
import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { completePasswordReset, requestPasswordResetCode, verifyPasswordResetCode } from "../api";
import styles from "../components/AuthForm.module.css";
import { EmailCodeFlow } from "../components/EmailCodeFlow";
import type { LoginPageState } from "../session/loginNotices";

const STEP_LABELS = ["验证邮箱", "输入验证码", "设置新密码"] as const;

/** 从登录页跳过来时带的信息（路由状态）。 */
export interface ResetPasswordPageState {
  email?: string;
}

/** 读取路由状态里的邮箱。路由状态来自浏览器历史，可能是任何内容，只接受字符串。 */
function readInitialEmail(state: unknown): string {
  if (typeof state !== "object" || state === null) {
    return "";
  }
  const { email } = state as Record<string, unknown>;
  return typeof email === "string" ? email : "";
}

/** 忘记密码页组件：标题、重置密码三步流程，以及“返回登录”链接。 */
export function ResetPasswordPage() {
  const navigate = useNavigate();
  const location = useLocation();
  // 只在第一次渲染时读取一次，作为邮箱框的初始内容。
  const [initialEmail] = useState(() => readInitialEmail(location.state));

  /** 重置完成：跳到登录页，带上邮箱和“密码已重置”提示的编号。 */
  function handleCompleted(email: string) {
    const state: LoginPageState = { email, notice: "password_reset" };
    // replace：按“后退”不会回到已经完成的重置流程。
    navigate("/login", { replace: true, state });
  }

  return (
    <div className={styles.page}>
      <div className={styles.heading}>
        <h2 className={styles.title}>重置密码</h2>
        <p className={styles.subtitle}>通过邮箱验证码设置新密码，账户和数据都会保留</p>
      </div>

      <EmailCodeFlow
        stepLabels={STEP_LABELS}
        stepsAriaLabel="重置密码进度"
        initialEmail={initialEmail}
        emailHint="填写注册时使用的邮箱，我们会发送 6 位验证码"
        emailSubmitLabel="获取验证码"
        passwordLabel="新密码"
        passwordSubmitLabel="重置密码"
        restartCode="password_reset_failed"
        requestCode={requestPasswordResetCode}
        verifyCode={verifyPasswordResetCode}
        complete={completePasswordReset}
        onCompleted={handleCompleted}
      />

      <p className={styles.footer}>
        想起密码了？<Link to="/login">返回登录</Link>
      </p>
    </div>
  );
}

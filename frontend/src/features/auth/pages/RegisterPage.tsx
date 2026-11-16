/**
 * 注册页（地址 /register，只限未登录）。
 *
 *   ① 验证邮箱：填邮箱 → 后端发验证码，返回 challenge_id
 *   ② 输入验证码：提交验证码 → 后端返回注册凭证（10 分钟有效、只能用一次）
 *   ③ 设置密码：提交密码和凭证 → 后端创建账户
 *   完成后跳到登录页，邮箱已填好，上方提示“注册成功，请登录”（2026-10-25 用户确认）。
 *
 * 三步流程本身由通用组件 EmailCodeFlow 负责（忘记密码页也用它），
 * 这个页面只提供“配置”：调用注册的三个接口、显示注册相关的文字、完成后去哪。
 */
import { Link, useNavigate } from "react-router";

import { completeRegistration, requestRegistrationCode, verifyRegistrationCode } from "../api";
import styles from "../components/AuthForm.module.css";
import { EmailCodeFlow } from "../components/EmailCodeFlow";
import type { LoginPageState } from "../session/loginNotices";

const STEP_LABELS = ["验证邮箱", "输入验证码", "设置密码"] as const;

/** 注册页组件：标题、注册三步流程，以及“去登录”链接。 */
export function RegisterPage() {
  const navigate = useNavigate();

  /** 注册完成：跳到登录页，带上邮箱和“注册成功”提示的编号。 */
  function handleCompleted(email: string) {
    const state: LoginPageState = { email, notice: "registered" };
    // replace：注册页从浏览器历史里替换掉，按“后退”不会回到已经完成的注册流程。
    navigate("/login", { replace: true, state });
  }

  return (
    <div className={styles.page}>
      <div className={styles.heading}>
        <h2 className={styles.title}>创建账号</h2>
        <p className={styles.subtitle}>用邮箱注册 ChainScope</p>
      </div>

      <EmailCodeFlow
        stepLabels={STEP_LABELS}
        stepsAriaLabel="注册进度"
        emailHint="我们会向这个邮箱发送 6 位验证码"
        emailSubmitLabel="获取验证码"
        passwordLabel="密码"
        passwordSubmitLabel="完成注册"
        restartCode="registration_failed"
        requestCode={requestRegistrationCode}
        verifyCode={verifyRegistrationCode}
        complete={completeRegistration}
        onCompleted={handleCompleted}
      />

      <p className={styles.footer}>
        已有账号？<Link to="/login">去登录</Link>
      </p>
    </div>
  );
}

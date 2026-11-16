/**
 * 登录页（地址 /login，只限未登录）。外观在实施第 3 步继续打磨。
 *
 * 流程：
 * 1. 用户填邮箱和密码，点“登录”；
 * 2. 先在前端检查有没有填（只是方便用户，真正的检查在后端）；
 * 3. 调用 useAuth().login()，它会请求 POST /api/auth/login；
 * 4. 成功：登录状态变为 authenticated，外层的 GuestOnly 自动跳到工作台（或登录前想去的页面）；
 *    失败：显示后端返回的中文提示和请求编号。
 *
 * 从其他页面跳过来时，会通过路由状态带来邮箱和提示编号（见 session/loginNotices.ts）：
 * - 注册成功：邮箱自动填好，上方显示“注册成功，请登录”，光标直接放在密码框；
 * - 退出登录、登录已失效：上方显示对应提示。
 * 路由状态保存在浏览器的历史记录里，不出现在网址中。
 *
 * 安全相关的显示规则：
 * - 账号不存在和密码错误，后端返回同一句“账号或密码错误”，这里原样显示，
 *   不能自己改成“该邮箱未注册”之类，否则别人可以借登录页探测谁注册过（方案第 4 节）；
 * - 尝试太多次被限速（rate_limited）时，按后端给的秒数倒计时，期间按钮不能点。
 */
import { useState, type FormEvent } from "react";
import { Link, useLocation } from "react-router";

import { asApiError, type ApiError } from "../../../shared/api/client";
import { Alert } from "../../../shared/ui/Alert";
import { Button } from "../../../shared/ui/Button";
import { PasswordField } from "../../../shared/ui/PasswordField";
import { TextField } from "../../../shared/ui/TextField";
import { useCountdown } from "../../../shared/ui/useCountdown";
import { EMAIL_MAX_LENGTH } from "../../../shared/validation/email";
import styles from "../components/AuthForm.module.css";
import { useAuth } from "../session/AuthContext";
import { LOGIN_NOTICES, readLoginPageState } from "../session/loginNotices";
import type { ResetPasswordPageState } from "./ResetPasswordPage";

/** 前端的“没填”提示；为空表示该项没问题。 */
interface FieldErrors {
  email?: string;
  password?: string;
}

/** 登录页组件：邮箱、密码两个输入框和登录按钮，上方显示提示，下方是“创建账号”链接。 */
export function LoginPage() {
  const { login } = useAuth();
  const location = useLocation();
  // 只在第一次渲染时读取一次路由状态，作为初始值。
  const [initial] = useState(() => readLoginPageState(location.state));
  const [email, setEmail] = useState(initial.email ?? "");
  const [password, setPassword] = useState("");
  // 提示的文字和颜色从 loginNotices.ts 查出；提交登录后收起。
  const [notice, setNotice] = useState(initial.notice ? LOGIN_NOTICES[initial.notice] : null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [error, setError] = useState<ApiError | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const countdown = useCountdown();
  const waiting = countdown.remaining > 0;

  /** 提交登录表单：先检查是否填写，再请求后端；按结果显示提示或交给 GuestOnly 跳转。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    // 阻止浏览器默认的“提交表单并刷新页面”，改由我们用 JavaScript 发请求。
    event.preventDefault();

    const nextErrors: FieldErrors = {};
    if (!email.trim()) {
      nextErrors.email = "请输入邮箱";
    }
    if (!password) {
      nextErrors.password = "请输入密码";
    }
    setFieldErrors(nextErrors);
    if (nextErrors.email || nextErrors.password) {
      return;
    }

    setSubmitting(true);
    setError(null);
    // 用户已经开始登录，“注册成功”的提示完成了使命，收起来，免得和错误提示同时出现。
    setNotice(null);
    try {
      // 邮箱去掉首尾空格（复制粘贴时常带上）；密码原样发送，空格也可能是密码的一部分。
      await login(email.trim(), password);
      // 成功后不需要在这里跳转：GuestOnly 看到已登录会自动跳走。
    } catch (caught) {
      const apiError = asApiError(caught, "登录时出现意外错误，请重试");
      setError(apiError);
      if (apiError.code === "rate_limited") {
        countdown.start(apiError.retryAfter);
      }
      // 失败后清空密码框，避免密码长时间留在页面上；邮箱保留，方便改正后重试。
      setPassword("");
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.heading}>
        <h2 className={styles.title}>登录 ChainScope</h2>
        <p className={styles.subtitle}>继续你的研究</p>
      </div>

      {notice && <Alert variant={notice.variant}>{notice.text}</Alert>}
      {error && (
        <Alert variant="error" requestId={error.requestId}>
          {error.message}
          {waiting && `（${countdown.text}后可以再试）`}
        </Alert>
      )}

      {/* noValidate：关掉浏览器自带的英文校验气泡，统一用我们自己的中文提示。 */}
      <form className={styles.form} onSubmit={(event) => void handleSubmit(event)} noValidate>
        <TextField
          label="邮箱"
          type="email"
          name="email"
          // username：告诉密码管理器这是登录用的账号，便于自动填充。
          autoComplete="username"
          inputMode="email"
          placeholder="you@example.com"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          error={fieldErrors.email}
          maxLength={EMAIL_MAX_LENGTH}
          // 光标放在第一个需要填的地方：邮箱已带过来就放在密码框，否则放在邮箱框。
          autoFocus={!initial.email}
        />
        <PasswordField
          label="密码"
          name="password"
          autoComplete="current-password"
          placeholder="请输入密码"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          error={fieldErrors.password}
          maxLength={1024}
          autoFocus={Boolean(initial.email)}
        />
        {/* 忘记密码：带上已经填的邮箱，到重置页不用再输一遍（邮箱只放在路由状态里，不进网址）。 */}
        <Link
          to="/reset-password"
          state={{ email: email.trim() } satisfies ResetPasswordPageState}
          className={styles.inlineLink}
        >
          忘记密码？
        </Link>
        <Button type="submit" block loading={submitting} disabled={waiting}>
          {waiting ? `请 ${countdown.text}后再试` : "登录"}
        </Button>
      </form>

      <p className={styles.footer}>
        还没有账号？<Link to="/register">创建账号</Link>
      </p>
    </div>
  );
}

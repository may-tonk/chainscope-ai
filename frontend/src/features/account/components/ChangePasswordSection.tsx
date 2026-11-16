/**
 * 设置页：修改密码。
 *
 * 需要输入当前密码和新密码。成功后后端让所有设备（包括这台）退出登录，
 * 页面跳到登录页并提示“密码已修改，请用新密码登录”。
 *
 * 前端先做的检查（只是方便用户，最终以后端为准）：
 * - 当前密码没填；
 * - 新密码长度不在 8～128 个字符（与后端同一套规则，见 shared/validation/password.ts）；
 * - 新密码和当前密码相同（改了等于没改）。
 */
import { useState, type FormEvent } from "react";

import { Alert } from "../../../shared/ui/Alert";
import { Button } from "../../../shared/ui/Button";
import { PasswordField } from "../../../shared/ui/PasswordField";
import {
  PASSWORD_MAX_LENGTH,
  newPasswordHint,
  validateNewPassword,
} from "../../../shared/validation/password";
import { useAuth } from "../../auth/session/AuthContext";
import { changePassword } from "../api";
import { SettingsSection } from "./SettingsSection";
import styles from "./SettingsSection.module.css";
import { useAccountAction, type AccountFieldErrors } from "./useAccountAction";

/** 修改密码分区组件。email 用于给密码管理器的隐藏账号字段。 */
export function ChangePasswordSection({ email }: { email: string }) {
  const { endSession } = useAuth();
  const action = useAccountAction();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const waiting = action.countdown.remaining > 0;

  /** 提交：先做前端检查，通过后请求后端；成功则结束会话（跳到登录页）。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const errors: AccountFieldErrors = {};
    if (!currentPassword) {
      errors.currentPassword = "请输入当前密码";
    }
    const newProblem = validateNewPassword(newPassword);
    if (newProblem) {
      errors.newPassword = newProblem;
    } else if (newPassword === currentPassword) {
      errors.newPassword = "新密码不能和当前密码相同";
    }
    if (errors.currentPassword || errors.newPassword) {
      action.setFieldErrors(errors);
      return;
    }
    const ok = await action.run(() => changePassword(currentPassword, newPassword));
    if (ok) {
      endSession("password_changed");
    } else {
      // 失败后清空两个密码框，避免密码长时间留在页面上。
      setCurrentPassword("");
      setNewPassword("");
    }
  }

  return (
    <SettingsSection
      title="修改密码"
      description="修改后，所有已登录的设备（包括这台）都会退出，需要用新密码重新登录。"
    >
      <form className={styles.form} onSubmit={(event) => void handleSubmit(event)} noValidate>
        {action.error && (
          <Alert variant="error" requestId={action.error.requestId}>
            {action.error.message}
            {waiting && `（${action.countdown.text}后可以再试）`}
          </Alert>
        )}
        {/* 给密码管理器看的账号：保存新密码时能对应到这个邮箱。屏幕上不显示。 */}
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
          label="当前密码"
          name="current-password"
          autoComplete="current-password"
          value={currentPassword}
          onChange={(event) => setCurrentPassword(event.target.value)}
          error={action.fieldErrors.currentPassword}
          maxLength={1024}
        />
        <PasswordField
          label="新密码"
          name="new-password"
          autoComplete="new-password"
          value={newPassword}
          onChange={(event) => setNewPassword(event.target.value)}
          hint={newPasswordHint(newPassword)}
          error={action.fieldErrors.newPassword}
          maxLength={PASSWORD_MAX_LENGTH * 4}
        />
        <div className={styles.actions}>
          <Button type="submit" loading={action.submitting} disabled={waiting}>
            {waiting ? `请 ${action.countdown.text}后再试` : "修改密码"}
          </Button>
        </div>
      </form>
    </SettingsSection>
  );
}

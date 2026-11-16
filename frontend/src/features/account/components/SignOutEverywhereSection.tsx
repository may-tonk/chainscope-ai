/**
 * 设置页：退出所有设备。
 *
 * 用途：在别人的电脑上登录过忘了退出，或怀疑账户在别处被登录时，一次让所有设备退出。
 * 需要输入当前密码（防止有人借你没锁屏的电脑，把你在所有设备上踢下线）。
 * 成功后这台设备也会退出，页面跳到登录页并提示“已退出所有设备，请重新登录”。
 */
import { useState, type FormEvent } from "react";

import { Alert } from "../../../shared/ui/Alert";
import { Button } from "../../../shared/ui/Button";
import { PasswordField } from "../../../shared/ui/PasswordField";
import { useAuth } from "../../auth/session/AuthContext";
import { signOutEverywhere } from "../api";
import { SettingsSection } from "./SettingsSection";
import styles from "./SettingsSection.module.css";
import { useAccountAction } from "./useAccountAction";

/** 退出所有设备分区组件。 */
export function SignOutEverywhereSection() {
  const { endSession } = useAuth();
  const action = useAccountAction();
  const [currentPassword, setCurrentPassword] = useState("");
  const waiting = action.countdown.remaining > 0;

  /** 提交：检查是否填了当前密码，再请求后端；成功则结束会话（跳到登录页）。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!currentPassword) {
      action.setFieldErrors({ currentPassword: "请输入当前密码" });
      return;
    }
    const ok = await action.run(() => signOutEverywhere(currentPassword));
    if (ok) {
      endSession("signed_out_everywhere");
    } else {
      setCurrentPassword("");
    }
  }

  return (
    <SettingsSection
      title="退出所有设备"
      description="让所有已登录的设备（包括这台）退出登录。在别人的电脑上登录过，或怀疑账户在别处被使用时可以用它。"
    >
      <form className={styles.form} onSubmit={(event) => void handleSubmit(event)} noValidate>
        {action.error && (
          <Alert variant="error" requestId={action.error.requestId}>
            {action.error.message}
            {waiting && `（${action.countdown.text}后可以再试）`}
          </Alert>
        )}
        <PasswordField
          label="当前密码"
          name="current-password"
          autoComplete="current-password"
          value={currentPassword}
          onChange={(event) => setCurrentPassword(event.target.value)}
          error={action.fieldErrors.currentPassword}
          maxLength={1024}
        />
        <div className={styles.actions}>
          <Button type="submit" variant="secondary" loading={action.submitting} disabled={waiting}>
            {waiting ? `请 ${action.countdown.text}后再试` : "退出所有设备"}
          </Button>
        </div>
      </form>
    </SettingsSection>
  );
}

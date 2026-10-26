/**
 * 设置页最下方的“危险区”：注销账户。
 *
 * 注销是立即、彻底的删除，不可恢复（方案 13.1 节，2026-10-23 用户确认）。为防止误操作，要确认两次：
 * 1. 输入当前密码，点“注销账户”（前端只检查是否填了密码）；
 * 2. 弹出确认对话框，写明后果，再点“确认注销”才真正请求后端。
 * 当前密码不对时关闭对话框，提示显示在密码框下方。
 * 成功后页面跳到登录页，提示“账户已注销，相关数据已删除”。
 */
import { useState, type FormEvent } from "react";

import { Alert } from "../../../shared/ui/Alert";
import { Button } from "../../../shared/ui/Button";
import { ConfirmDialog } from "../../../shared/ui/ConfirmDialog";
import { PasswordField } from "../../../shared/ui/PasswordField";
import { useAuth } from "../../auth/session/AuthContext";
import { deleteAccount } from "../api";
import { SettingsSection } from "./SettingsSection";
import styles from "./SettingsSection.module.css";
import { useAccountAction } from "./useAccountAction";

/** 注销账户分区组件（危险区）。 */
export function DeleteAccountSection() {
  const { endSession } = useAuth();
  const action = useAccountAction();
  const [currentPassword, setCurrentPassword] = useState("");
  const [confirming, setConfirming] = useState(false);
  const waiting = action.countdown.remaining > 0;

  /** 第一次确认：检查是否填了当前密码，通过后打开确认对话框（还不请求后端）。 */
  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!currentPassword) {
      action.setFieldErrors({ currentPassword: "请输入当前密码" });
      return;
    }
    action.setFieldErrors({});
    setConfirming(true);
  }

  /** 第二次确认：在对话框里点“确认注销”，才真正请求后端删除账户。 */
  async function handleConfirm() {
    const ok = await action.run(() => deleteAccount(currentPassword));
    if (ok) {
      endSession("account_deleted");
      return;
    }
    // 失败（例如当前密码不对）：关闭对话框，让用户看到密码框下方或上方的提示。
    setConfirming(false);
    setCurrentPassword("");
  }

  return (
    <SettingsSection
      danger
      title="注销账户"
      description={
        <>
          永久删除这个账户，<strong>不可恢复</strong>：
          <ul className={styles.consequences}>
            <li>所有设备立即退出登录；</li>
            <li>账户和相关数据被删除；</li>
            <li>同一邮箱以后可以重新注册，但不会继承原来的任何数据。</li>
          </ul>
        </>
      }
    >
      <form className={styles.form} onSubmit={handleSubmit} noValidate>
        {action.error && (
          <Alert variant="error" requestId={action.error.requestId}>
            {action.error.message}
            {waiting && `（${action.countdown.text}后可以再试）`}
          </Alert>
        )}
        <PasswordField
          label="当前密码"
          name="delete-current-password"
          autoComplete="current-password"
          value={currentPassword}
          onChange={(event) => setCurrentPassword(event.target.value)}
          error={action.fieldErrors.currentPassword}
          maxLength={1024}
        />
        <div className={styles.actions}>
          <Button type="submit" variant="danger" disabled={waiting}>
            {waiting ? `请 ${action.countdown.text}后再试` : "注销账户"}
          </Button>
        </div>
      </form>

      <ConfirmDialog
        open={confirming}
        title="确认注销账户？"
        confirmLabel="确认注销"
        variant="danger"
        loading={action.submitting}
        onConfirm={() => void handleConfirm()}
        onCancel={() => setConfirming(false)}
      >
        账户和相关数据会被立即、永久删除，无法恢复。确定要继续吗？
      </ConfirmDialog>
    </SettingsSection>
  );
}

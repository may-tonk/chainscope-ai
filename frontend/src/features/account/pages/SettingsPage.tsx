/**
 * 账户设置页（地址 /settings，需要登录）。
 *
 * 页面只负责把各个分区按顺序排好，每个分区的表单和逻辑在 components/ 里各占一个文件：
 *   账户信息（只读） → 修改密码 → 退出所有设备 → 注销账户（危险区，放在最下面）
 * 以后增加新的设置项，只需要写一个新的分区组件，放进这里的列表。
 */
import { useAuth } from "../../auth/session/AuthContext";
import { AccountInfoSection } from "../components/AccountInfoSection";
import { ChangePasswordSection } from "../components/ChangePasswordSection";
import { DeleteAccountSection } from "../components/DeleteAccountSection";
import { SignOutEverywhereSection } from "../components/SignOutEverywhereSection";
import styles from "./SettingsPage.module.css";

/** 账户设置页组件。 */
export function SettingsPage() {
  const { state } = useAuth();
  // 外层 RequireAuth 保证这里已登录；再判断一次是让 TypeScript 知道 account 一定存在。
  if (state.status !== "authenticated") {
    return null;
  }
  const { account } = state;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>设置</h1>
        <p className={styles.subtitle}>管理账户信息和登录安全。</p>
      </header>

      <AccountInfoSection account={account} />
      <ChangePasswordSection email={account.email} />
      <SignOutEverywhereSection />
      <DeleteAccountSection />
    </div>
  );
}

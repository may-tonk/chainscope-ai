/**
 * 设置页第一块：账户信息（只读）——邮箱和注册时间。
 * 第一版不支持更换邮箱（方案第 2 节），所以这里只显示、不提供修改。
 */
import { formatDate } from "../../../shared/format/date";
import type { Account } from "../../auth/api";
import { SettingsSection } from "./SettingsSection";
import styles from "./SettingsSection.module.css";

/** 账户信息分区组件。 */
export function AccountInfoSection({ account }: { account: Account }) {
  return (
    <SettingsSection title="账户信息" description="登录使用的邮箱和注册时间。第一版暂不支持更换邮箱。">
      <dl className={styles.facts}>
        <div>
          <dt>邮箱</dt>
          <dd>{account.email}</dd>
        </div>
        <div>
          <dt>注册时间</dt>
          <dd>{formatDate(account.created_at)}</dd>
        </div>
      </dl>
    </SettingsSection>
  );
}

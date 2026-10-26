/**
 * 工作台首页（地址 /，需要登录）。参照参考图“我的研究库”的标题区。
 *
 * 页面内容：
 * 1. 标题区：“研究空间”和一句说明；
 * 2. 账户卡片：邮箱、注册时间；
 * 3. “即将上线”：还没完成的功能，每个一张卡片（图标、名称、一句话说明）。
 *
 * 第一版不放假的报告卡片或示例数据：只用模拟数据做出来的界面，会让人误以为功能已经可用（方案 3.4）。
 *
 * 可扩展性：“即将上线”的卡片不是写死在这里的，而是由外层（app/router.tsx）从导航配置
 * （app/navigation.ts）生成后传进来。某个功能上线时只改导航配置的一个字段，
 * 侧栏和首页同时更新。首页也因此不需要知道 app 层的配置（依赖方向保持 app → features）。
 */
import type { LucideIcon } from "lucide-react";

import { Link } from "react-router";

import { formatDate } from "../../../shared/format/date";
import { useAuth } from "../../auth/session/AuthContext";
import styles from "./HomePage.module.css";

/** 首页“即将上线”卡片需要的信息。导航配置里的 NavItem 正好包含这些字段，可以直接传入。 */
export interface FeaturePreview {
  id: string;
  label: string;
  description: string;
  icon: LucideIcon;
}

interface HomePageProps {
  /** 还没上线的功能，按显示顺序排列；为空时不显示“即将上线”这一块。 */
  upcoming: FeaturePreview[];
}

/** 工作台首页组件：标题区、账户卡片、“即将上线”的功能卡片。 */
export function HomePage({ upcoming }: HomePageProps) {
  const { state } = useAuth();
  // 外层 RequireAuth 保证这里已登录；再判断一次是让 TypeScript 知道 account 一定存在。
  if (state.status !== "authenticated") {
    return null;
  }
  const { account } = state;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>研究空间</h1>
        <p className={styles.subtitle}>把每次研究，积累成自己的研究方法。</p>
      </header>

      <section className={styles.card} aria-labelledby="account-heading">
        <div className={styles.cardHeader}>
          <h2 id="account-heading" className={styles.cardTitle}>
            账户
          </h2>
          {/* 账户相关的操作（改密码、退出所有设备、注销）都在设置页。 */}
          <Link to="/settings" className={styles.cardLink}>
            账户设置
          </Link>
        </div>
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
      </section>

      {upcoming.length > 0 && (
        <section className={styles.upcoming} aria-labelledby="upcoming-heading">
          <div className={styles.sectionHeading}>
            <h2 id="upcoming-heading" className={styles.cardTitle}>
              即将上线
            </h2>
            <p className={styles.sectionText}>研究功能正在建设中，下面这些模块会陆续开放。</p>
          </div>
          <ul className={styles.featureGrid}>
            {upcoming.map(({ id, label, description, icon: Icon }) => (
              <li key={id} className={styles.featureCard}>
                <span className={styles.featureIcon}>
                  <Icon size={20} aria-hidden="true" />
                </span>
                <div className={styles.featureBody}>
                  <p className={styles.featureTitle}>
                    {label}
                    <span className={styles.badge}>即将上线</span>
                  </p>
                  <p className={styles.featureText}>{description}</p>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

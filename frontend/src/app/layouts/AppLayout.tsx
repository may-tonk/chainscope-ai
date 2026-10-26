/**
 * 工作台外壳：登录后所有页面共用的框架（参照参考图“我的研究库”“BEM 研究概览”）。
 *
 *   ┌────────────┬──────────────────────────┐
 *   │ ChainScope │ 顶栏：页面名称             │
 *   │ [新建研究]  ├──────────────────────────┤
 *   │ 全局导航    │                          │
 *   │ 板块导航    │   页面内容（<Outlet />）   │
 *   │ …          │                          │
 *   │ 用户区      │                          │
 *   └────────────┴──────────────────────────┘
 *
 * 手机等窄屏上左侧栏默认隐藏，点顶栏左边的菜单按钮滑出，点遮罩或任一入口收起。
 *
 * 外壳本身不写死任何具体功能：导航来自 app/navigation.ts，
 * 页面名称和板块导航来自当前路由的 handle（RouteHandle）。
 */
import { LogOut, Menu, Plus, X } from "lucide-react";
import { useState } from "react";
import { Link, NavLink, Outlet } from "react-router";

import { useAuth } from "../../features/auth/session/AuthContext";
import { asApiError } from "../../shared/api/client";
import { accountNavigation, primaryAction, primaryNavigation } from "../navigation";
import { useRouteHandle } from "../useRouteHandle";
import styles from "./AppLayout.module.css";
import { NavList } from "./NavList";

/** 工作台外壳组件：左侧栏（导航、用户区）+ 右侧顶栏和页面内容。只在已登录时渲染。 */
export function AppLayout() {
  const { state, logout } = useAuth();
  const handle = useRouteHandle();
  // 窄屏上左侧栏是否展开。宽屏上始终显示，这个值不起作用。
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);

  // RequireAuth 保证进入这里时一定已登录；这里再判断一次，让 TypeScript 知道 account 存在。
  if (state.status !== "authenticated") {
    return null;
  }
  const { email } = state.account;
  // 头像显示邮箱的第一个字符（大写）。用户名需要改数据库，第一版先用邮箱（2026-10-25 用户确认）。
  const initial = email.charAt(0).toUpperCase();

  /** 点击“退出登录”：请求后端退出；失败时在用户区下方显示原因，按钮恢复可点。 */
  async function handleLogout() {
    setLoggingOut(true);
    setLogoutError(null);
    try {
      await logout();
      // 成功后登录状态变为 anonymous（原因 signed_out），RequireAuth 会自动跳到登录页，
      // 这里不用处理跳转。退出后登录页不显示提示（2026-10-26 用户决定，见 loginNotices.ts）。
    } catch (error) {
      setLogoutError(asApiError(error, "退出失败，请重试").message);
      setLoggingOut(false);
    }
  }

  const closeSidebar = () => setSidebarOpen(false);
  const Settings = accountNavigation.icon;

  return (
    <div className={styles.shell}>
      {/* 窄屏上侧栏展开时的半透明遮罩，点击收起侧栏。 */}
      {sidebarOpen && <div className={styles.backdrop} onClick={closeSidebar} aria-hidden="true" />}

      <aside
        id="app-sidebar"
        className={sidebarOpen ? `${styles.sidebar} ${styles.sidebarOpen}` : styles.sidebar}
      >
        <div className={styles.sidebarHeader}>
          <Link to="/" className={styles.brand} onClick={closeSidebar}>
            ChainScope
          </Link>
          <button
            type="button"
            className={styles.closeButton}
            onClick={closeSidebar}
            aria-label="收起菜单"
          >
            <X size={20} aria-hidden="true" />
          </button>
        </div>

        {/* 主按钮：研究功能上线前不可用。 */}
        <button type="button" className={styles.primaryAction} disabled={!primaryAction.available}>
          <Plus size={18} aria-hidden="true" />
          <span>{primaryAction.label}</span>
          {!primaryAction.available && <span className={styles.soon}>即将上线</span>}
        </button>

        <nav className={styles.nav} aria-label="主导航">
          <NavList group={primaryNavigation} onNavigate={closeSidebar} />
          {handle.sectionNavigation && (
            <>
              <hr className={styles.divider} />
              <NavList group={handle.sectionNavigation} onNavigate={closeSidebar} />
            </>
          )}
        </nav>

        {/* 左下角用户区：头像（邮箱首字母）、邮箱、设置和退出。 */}
        <div className={styles.user}>
          <div className={styles.userInfo}>
            <span className={styles.avatar} aria-hidden="true">
              {initial}
            </span>
            {/* 邮箱过长时用省略号截断，鼠标悬停显示完整邮箱。 */}
            <span className={styles.email} title={email}>
              {email}
            </span>
          </div>
          <div className={styles.userActions}>
            {accountNavigation.available ? (
              // NavLink：当前就在设置页时自动高亮，和侧栏导航的选中效果一致。
              <NavLink
                to={accountNavigation.path}
                className={({ isActive }) =>
                  isActive ? `${styles.userAction} ${styles.userActionActive}` : styles.userAction
                }
                onClick={closeSidebar}
              >
                <Settings size={16} aria-hidden="true" />
                {accountNavigation.label}
              </NavLink>
            ) : (
              <span className={`${styles.userAction} ${styles.userActionDisabled}`} aria-disabled="true">
                <Settings size={16} aria-hidden="true" />
                {accountNavigation.label}
              </span>
            )}
            <button
              type="button"
              className={styles.userAction}
              onClick={() => void handleLogout()}
              disabled={loggingOut}
            >
              <LogOut size={16} aria-hidden="true" />
              {loggingOut ? "正在退出…" : "退出登录"}
            </button>
          </div>
          {logoutError && (
            <p className={styles.userError} role="alert">
              {logoutError}
            </p>
          )}
        </div>
      </aside>

      <div className={styles.main}>
        <header className={styles.topbar}>
          {/* 内层与页面内容同宽、同样居中，顶栏文字和下方标题左对齐。 */}
          <div className={styles.topbarInner}>
            <button
              type="button"
              className={styles.menuButton}
              onClick={() => setSidebarOpen(true)}
              aria-label="打开菜单"
              aria-controls="app-sidebar"
              aria-expanded={sidebarOpen}
            >
              <Menu size={20} aria-hidden="true" />
            </button>
            {/* 页面名称；以后在这里扩展为“研究 / BEM”这样的路径导航，右侧留给搜索框。 */}
            <span className={styles.pageTitle}>{handle.title ?? "ChainScope"}</span>
          </div>
        </header>
        <main className={styles.content}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

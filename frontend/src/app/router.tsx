/**
 * 路由表：网址和页面的对应关系。
 *
 * 结构：
 *   根布局 RootLayout（所有页面：同步浏览器标签页标题）
 *   ├ 认证外壳 AuthLayout（只限未登录）：/login、/register、/reset-password
 *   ├ 工作台外壳 AppLayout（需要登录）：/、/settings；以后加入各研究功能
 *   └ 其他任何地址：404 页面
 *
 * 新增页面时：
 * 1. 在对应外壳的 children 里加一项，handle 里写上 title（标签页标题和顶栏标题都从这里来），
 *    需要板块导航时再写 sectionNavigation（RouteHandle，见 navigation.ts）；
 * 2. 如果要出现在左侧栏，到 app/navigation.ts 把对应入口改为 available: true。
 *
 * routes 单独导出，测试里可以用 createMemoryRouter(routes) 在内存中模拟各种网址。
 */
import type { RouteObject } from "react-router";

import { SettingsPage } from "../features/account/pages/SettingsPage";
import { LoginPage } from "../features/auth/pages/LoginPage";
import { RegisterPage } from "../features/auth/pages/RegisterPage";
import { ResetPasswordPage } from "../features/auth/pages/ResetPasswordPage";
import { GuestOnly, RequireAuth } from "../features/auth/session/guards";
import { HomePage } from "../features/workspace/pages/HomePage";
import { AppLayout } from "./layouts/AppLayout";
import { AuthLayout } from "./layouts/AuthLayout";
import { RootLayout } from "./layouts/RootLayout";
import { upcomingFeatures, type RouteHandle } from "./navigation";
import { NotFoundPage } from "./NotFoundPage";

export const routes: RouteObject[] = [
  {
    element: <RootLayout />,
    children: [
      {
        // 没有 path 的“布局路由”：只负责套上外壳和访问保护，网址由 children 决定。
        element: (
          <GuestOnly>
            <AuthLayout />
          </GuestOnly>
        ),
        children: [
          { path: "/login", element: <LoginPage />, handle: { title: "登录" } satisfies RouteHandle },
          {
            path: "/register",
            element: <RegisterPage />,
            handle: { title: "创建账号" } satisfies RouteHandle,
          },
          {
            path: "/reset-password",
            element: <ResetPasswordPage />,
            handle: { title: "重置密码" } satisfies RouteHandle,
          },
        ],
      },
      {
        element: (
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        ),
        children: [
          {
            path: "/",
            // “即将上线”的卡片由导航配置生成，首页本身不写死功能列表。
            element: <HomePage upcoming={upcomingFeatures()} />,
            handle: { title: "研究空间" } satisfies RouteHandle,
          },
          {
            path: "/settings",
            element: <SettingsPage />,
            handle: { title: "设置" } satisfies RouteHandle,
          },
        ],
      },
      { path: "*", element: <NotFoundPage />, handle: { title: "页面不存在" } satisfies RouteHandle },
    ],
  },
];

/**
 * 应用的最外层：把各部分按顺序套起来。
 *
 *   AuthProvider     维护全局登录状态（打开网站时向后端确认一次）
 *   └ SessionGate    确认完成前显示加载画面；连不上后端时显示错误和“重试”
 *     └ RouterProvider  按网址显示对应页面（路由表见 router.tsx）
 *
 * 顺序有讲究：路由里的页面保护（RequireAuth / GuestOnly）要读取登录状态，
 * 所以 AuthProvider 必须在路由外面；SessionGate 保证页面保护拿到的一定是确认过的状态。
 */
import { createBrowserRouter } from "react-router";
import { RouterProvider } from "react-router/dom";

import { AuthProvider } from "../features/auth/session/AuthProvider";
import { SessionGate } from "../features/auth/session/SessionGate";
import { routes } from "./router";

// 浏览器路由：使用真实的网址（/login、/settings），而不是 #/login 这种带井号的网址。
// 在模块加载时创建一次即可，整个应用共用。
const router = createBrowserRouter(routes);

/** 应用根组件：登录状态 → 状态确认关口 → 路由，三层依次包裹。 */
export function App() {
  return (
    <AuthProvider>
      <SessionGate>
        <RouterProvider router={router} />
      </SessionGate>
    </AuthProvider>
  );
}

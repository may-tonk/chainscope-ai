/**
 * 页面访问保护：决定某个页面在“已登录 / 未登录”时能不能打开。
 *
 * 注意：这只是为了使用体验（不让用户看到打不开的页面），不是安全措施。
 * 真正的权限检查在后端：每个需要登录的接口都会核对会话，前端被绕过也拿不到数据。
 *
 * 这两个组件只在 SessionGate 确认完登录状态之后才会渲染，
 * 所以这里只需要处理 anonymous 和 authenticated 两种状态。
 */
import type { ReactNode } from "react";
import { Navigate, useLocation, useSearchParams } from "react-router";

import { useAuth } from "./AuthContext";
import { isLoginNoticeKey, type LoginPageState } from "./loginNotices";
import { NEXT_PARAM, loginPathFor, safeNextPath } from "./redirect";

/**
 * 只有已登录才能看的页面（工作台、设置等）。
 * 未登录时跳到登录页，并记下当前地址，登录成功后回到这里。
 * 如果未登录有原因（例如登录已失效），并且提示表里有同名的提示，就让登录页显示它（见 loginNotices.ts）。
 * 目前 expired 有提示，signed_out（用户自己退出）没有提示，退出后直接回到干净的登录页。
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  const location = useLocation();
  if (state.status !== "authenticated") {
    const current = location.pathname + location.search + location.hash;
    const reason = state.status === "anonymous" ? state.reason : undefined;
    // 退出原因与提示编号同名；提示表里有这一项才传给登录页，没有就不显示任何提示。
    const loginState: LoginPageState | undefined = isLoginNoticeKey(reason)
      ? { notice: reason }
      : undefined;
    // replace：替换当前历史记录，按浏览器“后退”不会又回到这个打不开的页面。
    return <Navigate to={loginPathFor(current)} replace state={loginState} />;
  }
  return children;
}

/**
 * 只有未登录才需要看的页面（登录、注册、忘记密码）。
 * 已登录时直接跳走：有 next 参数且安全就去 next，否则去工作台首页。
 * 登录成功后状态变为 authenticated，这个组件会自动完成“登录后跳转”，登录页本身不用处理。
 */
export function GuestOnly({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  const [params] = useSearchParams();
  if (state.status === "authenticated") {
    return <Navigate to={safeNextPath(params.get(NEXT_PARAM))} replace />;
  }
  return children;
}

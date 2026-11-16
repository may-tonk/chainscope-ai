/**
 * 全局登录状态的维护者，包在整个应用的最外层（见 app/App.tsx）。
 *
 * 打开网站时它会：
 * 1. 请求 /api/auth/csrf：拿到 CSRF 令牌，并得知是否已登录（没有会话时后端顺便建匿名会话）；
 * 2. 如果已登录，再请求 /api/auth/me 取账户信息。
 * 确认完成前状态是 loading，由 SessionGate 显示加载画面，页面不会先闪一下登录页再跳走。
 *
 * 登录之后，还负责发现“登录已失效”，统一切换为未登录（原因 expired），由页面保护跳到登录页：
 * - 任何请求收到 not_authenticated：通过请求模块的 onSessionExpired 通知得知；
 * - 用户离开页面一段时间后回来：重新向后端确认一次（会话可能在这期间过期或在别处被退出）。
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError, asApiError, onSessionExpired, setCsrfToken } from "../../../shared/api/client";
import * as authApi from "../api";
import {
  AuthContext,
  type AuthContextValue,
  type AuthState,
  type SignedOutReason,
} from "./AuthContext";

/**
 * 回到页面时，距离上次确认超过这个时长才重新确认，避免频繁切换窗口时反复请求后端。
 * 后端已登录会话的空闲上限是 24 小时，5 分钟足够及时。
 */
const REVALIDATE_AFTER_MS = 5 * 60 * 1000;

/**
 * 向后端确认当前的登录状态，返回对应的 AuthState。这个函数本身不会抛错：
 * - 没登录、或者 me 返回 not_authenticated（会话刚好过期）→ anonymous；
 * - 其他任何失败（后端没启动、数据库不可用）→ error，让用户看到原因并可以重试。
 */
async function resolveSession(): Promise<AuthState> {
  try {
    const status = await authApi.fetchSessionStatus();
    if (!status.authenticated) {
      return { status: "anonymous" };
    }
    const account = await authApi.fetchMe();
    return { status: "authenticated", account };
  } catch (error) {
    if (error instanceof ApiError && error.code === "not_authenticated") {
      return { status: "anonymous" };
    }
    return { status: "error", error: asApiError(error, "页面出现意外错误，请刷新后重试") };
  }
}

/** 登录状态提供者组件：保存登录状态，并把状态和登录、退出、重试操作提供给 children。 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  // 打开网站时确认一次登录状态。
  useEffect(() => {
    // cancelled：组件已卸载（或开发模式下 React 故意重复执行一次）时，丢弃过时的结果。
    let cancelled = false;
    void resolveSession().then((next) => {
      if (!cancelled) {
        setState(next);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // 任何请求发现“登录已失效”时，切换为未登录（原因 expired）。
  // 只在原本已登录时才切换：打开网站时确认登录状态的过程中，也可能收到这个通知，那时不需要处理。
  useEffect(
    () =>
      onSessionExpired(() => {
        setState((previous) =>
          previous.status === "authenticated" ? { status: "anonymous", reason: "expired" } : previous,
        );
      }),
    [],
  );

  // 已登录时，用户离开页面一段时间后回来，重新确认一次登录状态。
  const authenticated = state.status === "authenticated";
  useEffect(() => {
    if (!authenticated) {
      return;
    }
    let lastChecked = Date.now();
    /** 页面重新变为可见时调用；距上次确认不到 5 分钟就跳过。 */
    const handleVisible = () => {
      if (document.visibilityState !== "visible" || Date.now() - lastChecked < REVALIDATE_AFTER_MS) {
        return;
      }
      lastChecked = Date.now();
      authApi
        .fetchSessionStatus()
        .then((status) => {
          if (!status.authenticated) {
            setState({ status: "anonymous", reason: "expired" });
          }
        })
        // 网络暂时不通时不打扰用户：保持当前状态，下次真正发请求时自然会知道结果。
        .catch(() => {});
    };
    document.addEventListener("visibilitychange", handleVisible);
    return () => document.removeEventListener("visibilitychange", handleVisible);
  }, [authenticated]);

  const reload = useCallback(async () => {
    setState({ status: "loading" });
    setState(await resolveSession());
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    // 失败时 authApi.login 抛出 ApiError，这里不捕获，交给登录页显示提示；状态保持不变。
    const account = await authApi.login(email, password);
    setState({ status: "authenticated", account });
  }, []);

  const logout = useCallback(async () => {
    // 先等后端确认退出成功，再把页面切换为未登录。
    // 如果网络失败，后端的会话其实还有效，不能让用户误以为已经退出（例如在公共电脑上）。
    await authApi.logout();
    setState({ status: "anonymous", reason: "signed_out" });
  }, []);

  const endSession = useCallback((reason: SignedOutReason) => {
    // 后端已经撤销了会话、清除了 Cookie，旧的 CSRF 令牌也随之作废；下次修改类请求前会重新获取。
    setCsrfToken(null);
    setState({ status: "anonymous", reason });
  }, []);

  // useMemo：只有状态或操作变化时才生成新对象，避免所有使用 useAuth() 的组件无谓地重新渲染。
  const value = useMemo<AuthContextValue>(
    () => ({ state, login, logout, reload, endSession }),
    [state, login, logout, reload, endSession],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

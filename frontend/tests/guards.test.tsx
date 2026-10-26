/**
 * 页面访问保护（src/features/auth/session/guards.tsx）的测试。
 *
 * 不经过真实的登录流程，而是直接给出一个“假的登录状态”，
 * 检查在不同状态、不同网址下，最终显示的是哪个页面、网址变成了什么。
 */
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { AuthContext, type AuthContextValue, type AuthState } from "../src/features/auth/session/AuthContext";
import { GuestOnly, RequireAuth } from "../src/features/auth/session/guards";

const ACCOUNT = { user_id: "u-1", email: "a@example.com", created_at: "2026-10-25T00:00:00Z" };

/** 用给定的登录状态和起始网址渲染一个最小的路由表，返回 router 以便检查最终网址。 */
function renderAt(state: AuthState, url: string) {
  const value: AuthContextValue = {
    state,
    login: async () => {},
    logout: async () => {},
    reload: async () => {},
    endSession: () => {},
  };
  const router = createMemoryRouter(
    [
      { path: "/login", element: <GuestOnly><p>登录页</p></GuestOnly> },
      { path: "/", element: <RequireAuth><p>首页</p></RequireAuth> },
      { path: "/settings", element: <RequireAuth><p>设置页</p></RequireAuth> },
    ],
    { initialEntries: [url] },
  );
  render(
    <AuthContext.Provider value={value}>
      <RouterProvider router={router} />
    </AuthContext.Provider>,
  );
  return router;
}

describe("RequireAuth：需要登录的页面", () => {
  it("未登录访问 /settings：跳到登录页，并记下原来的地址", () => {
    const router = renderAt({ status: "anonymous" }, "/settings");

    expect(screen.getByText("登录页")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(router.state.location.search).toBe("?next=%2Fsettings");
  });

  it("已登录可以正常打开", () => {
    renderAt({ status: "authenticated", account: ACCOUNT }, "/settings");

    expect(screen.getByText("设置页")).toBeInTheDocument();
  });
});

describe("GuestOnly：只限未登录的页面", () => {
  it("未登录时正常显示登录页", () => {
    renderAt({ status: "anonymous" }, "/login");

    expect(screen.getByText("登录页")).toBeInTheDocument();
  });

  it("已登录且 next 是站内地址：跳回原来想去的页面", () => {
    const router = renderAt({ status: "authenticated", account: ACCOUNT }, "/login?next=%2Fsettings");

    expect(screen.getByText("设置页")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings");
  });

  it.each(["https%3A%2F%2Fevil.example", "%2F%2Fevil.example"])(
    "已登录但 next=%s 指向外部：不跟随，回到首页",
    (next) => {
      const router = renderAt({ status: "authenticated", account: ACCOUNT }, `/login?next=${next}`);

      expect(screen.getByText("首页")).toBeInTheDocument();
      expect(router.state.location.pathname).toBe("/");
    },
  );
});

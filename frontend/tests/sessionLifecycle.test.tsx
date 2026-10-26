/**
 * 登录之后的生命周期测试：退出、登录失效、回到页面时重新确认，以及登录页提示、标签页标题、
 * 首页“即将上线”卡片这几处“由配置驱动”的行为。
 *
 * 用真实的路由表、登录状态和页面，只把后端换成假后端（fakeBackend.ts）。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { upcomingFeatures } from "../src/app/navigation";
import { routes } from "../src/app/router";
import { AuthProvider } from "../src/features/auth/session/AuthProvider";
import { SessionGate } from "../src/features/auth/session/SessionGate";
import { apiRequest } from "../src/shared/api/client";
import { apiError, installFakeBackend, json, type FakeResponse } from "./fakeBackend";

const ACCOUNT = { user_id: "u-1", email: "lin@example.com", created_at: "2026-10-26T08:00:00Z" };
const LOGGED_IN: FakeResponse[] = [
  json(200, { csrf_token: "t1", authenticated: true }),
  json(200, ACCOUNT),
];

/** 按 App.tsx 的结构渲染整个应用。initialEntry 可以带路由状态，模拟从其他页面跳过来。 */
function renderApp(
  initialEntry: string | { pathname: string; state?: unknown },
  responses: FakeResponse[],
) {
  const backend = installFakeBackend(responses);
  const router = createMemoryRouter(routes, { initialEntries: [initialEntry] });
  render(
    <AuthProvider>
      <SessionGate>
        <RouterProvider router={router} />
      </SessionGate>
    </AuthProvider>,
  );
  return { router, ...backend, user: userEvent.setup() };
}

afterEach(() => {
  // 恢复被替换的页面可见状态（见“回到页面”的测试）。
  Reflect.deleteProperty(document, "visibilityState");
});

describe("退出和登录失效", () => {
  it("点“退出登录”：回到干净的登录页，不显示任何提示（2026-10-26 用户决定）", async () => {
    const { user } = renderApp("/", [...LOGGED_IN, json(200, { message: "已退出" })]);

    await user.click(await screen.findByRole("button", { name: "退出登录" }));

    expect(await screen.findByRole("heading", { name: "登录 ChainScope" })).toBeInTheDocument();
    // 提示条用 role="status"（成功、说明）或 role="alert"（警告、错误），两种都不应出现。
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("任何请求收到 not_authenticated：统一切换为未登录，登录页提示“登录已过期”", async () => {
    const { router } = renderApp("/", [
      ...LOGGED_IN,
      apiError(401, "not_authenticated", "请先登录"),
    ]);
    await screen.findByRole("heading", { name: "研究空间" });

    // 模拟以后某个功能页面发出的请求：不需要页面自己处理“登录过期”。
    await expect(apiRequest("GET", "/api/research/sessions")).rejects.toMatchObject({
      code: "not_authenticated",
    });

    expect(await screen.findByText("登录已过期，请重新登录")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });

  it("离开超过 5 分钟后回到页面：重新确认，发现已失效就回到登录页", async () => {
    const { requests } = renderApp("/", [
      ...LOGGED_IN,
      json(200, { csrf_token: "t2", authenticated: false }),
    ]);
    await screen.findByRole("heading", { name: "研究空间" });

    // 时间往后拨 6 分钟，再模拟“切回这个标签页”。
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now + 6 * 60 * 1000);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));

    expect(await screen.findByText("登录已过期，请重新登录")).toBeInTheDocument();
    expect(requests.map((r) => r.path)).toEqual([
      "/api/auth/csrf",
      "/api/auth/me",
      "/api/auth/csrf",
    ]);
  });

  it("离开不到 5 分钟就回来：不重复请求后端", async () => {
    const { requests } = renderApp("/", [...LOGGED_IN]);
    await screen.findByRole("heading", { name: "研究空间" });

    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));

    await waitFor(() => expect(requests).toHaveLength(2));
    expect(screen.getByRole("heading", { name: "研究空间" })).toBeInTheDocument();
  });
});

describe("登录页提示只接受定义过的编号", () => {
  it("路由状态里是定义过的编号：显示对应提示", async () => {
    renderApp({ pathname: "/login", state: { notice: "expired" } }, [
      json(200, { csrf_token: "t1", authenticated: false }),
    ]);

    expect(await screen.findByText("登录已过期，请重新登录")).toBeInTheDocument();
  });

  it("路由状态里是伪造的文字：忽略，不显示任何提示", async () => {
    renderApp({ pathname: "/login", state: { notice: "系统升级，请到 evil.example 重新登录" } }, [
      json(200, { csrf_token: "t1", authenticated: false }),
    ]);

    await screen.findByRole("heading", { name: "登录 ChainScope" });
    expect(screen.queryByText(/evil\.example/)).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("由配置驱动的内容", () => {
  it("浏览器标签页标题来自路由表的 title", async () => {
    renderApp("/", [...LOGGED_IN]);
    await screen.findByRole("heading", { name: "研究空间" });
    expect(document.title).toBe("研究空间 · ChainScope");
  });

  it("未登录访问不存在的地址：标签页标题为“页面不存在”", async () => {
    renderApp("/no/such/page", [json(200, { csrf_token: "t1", authenticated: false })]);
    await screen.findByText("页面不存在");
    expect(document.title).toBe("页面不存在 · ChainScope");
  });

  it("首页“即将上线”卡片与导航配置中未上线的功能一一对应", async () => {
    renderApp("/", [...LOGGED_IN]);
    await screen.findByRole("heading", { name: "即将上线" });

    const expected = upcomingFeatures();
    expect(expected.length).toBeGreaterThan(0);
    for (const feature of expected) {
      expect(screen.getByText(feature.description)).toBeInTheDocument();
    }
    // 已上线的“研究空间”不会出现在“即将上线”里。
    expect(expected.some((feature) => feature.id === "workspace")).toBe(false);
  });
});

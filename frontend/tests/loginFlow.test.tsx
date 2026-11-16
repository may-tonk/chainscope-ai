/**
 * 登录流程的整体测试：用真实的路由表、登录状态和页面，只把后端换成假后端（fakeBackend.ts）。
 *
 * 覆盖：打开网站时确认登录状态、登录失败的提示、限速倒计时、登录成功后进入工作台、
 * 后端连不上时显示错误页而不是假装“未登录”。
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { routes } from "../src/app/router";
import { AuthProvider } from "../src/features/auth/session/AuthProvider";
import { SessionGate } from "../src/features/auth/session/SessionGate";
import { apiError, installFakeBackend, json, type FakeResponse } from "./fakeBackend";

const ACCOUNT = { user_id: "u-1", email: "lin@example.com", created_at: "2026-10-25T08:00:00Z" };

/** 按 App.tsx 的结构渲染整个应用，起始网址为 url。 */
function renderApp(url: string, responses: FakeResponse[]) {
  const backend = installFakeBackend(responses);
  const router = createMemoryRouter(routes, { initialEntries: [url] });
  render(
    <AuthProvider>
      <SessionGate>
        <RouterProvider router={router} />
      </SessionGate>
    </AuthProvider>,
  );
  return { router, ...backend };
}

/** 在登录表单里填写邮箱和密码并提交。 */
async function submitLogin(email: string, password: string) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("邮箱"), email);
  await user.type(screen.getByLabelText("密码"), password);
  await user.click(screen.getByRole("button", { name: "登录" }));
}

describe("打开网站", () => {
  it("未登录访问首页：确认状态后显示登录页", async () => {
    const { router } = renderApp("/", [json(200, { csrf_token: "t1", authenticated: false })]);

    expect(await screen.findByRole("heading", { name: "登录 ChainScope" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });

  it("已登录（刷新页面后）：取账户信息并直接显示工作台", async () => {
    const { requests } = renderApp("/", [
      json(200, { csrf_token: "t1", authenticated: true }),
      json(200, ACCOUNT),
    ]);

    expect(await screen.findByRole("heading", { name: "研究空间" })).toBeInTheDocument();
    expect(screen.getAllByText("lin@example.com").length).toBeGreaterThan(0);
    expect(requests.map((r) => r.path)).toEqual(["/api/auth/csrf", "/api/auth/me"]);
  });

  it("后端连不上：显示错误和“重试”，而不是把用户当成未登录", async () => {
    renderApp("/", [{ status: 502, body: "Bad Gateway", text: true }]);

    expect(await screen.findByText("暂时无法连接 ChainScope")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "登录 ChainScope" })).not.toBeInTheDocument();
  });
});

describe("登录", () => {
  it("没填就提交：前端提示，不发请求", async () => {
    const { requests } = renderApp("/login", [json(200, { csrf_token: "t1", authenticated: false })]);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "登录" }));

    expect(screen.getByText("请输入邮箱")).toBeInTheDocument();
    expect(screen.getByText("请输入密码")).toBeInTheDocument();
    expect(requests).toHaveLength(1); // 只有打开网站时的那一次
  });

  it("账号或密码错误：原样显示后端提示和请求编号，清空密码、保留邮箱", async () => {
    const { requests } = renderApp("/login", [
      json(200, { csrf_token: "t1", authenticated: false }),
      apiError(401, "authentication_failed", "账号或密码错误"),
    ]);

    await submitLogin("lin@example.com", "wrong-password");

    expect(await screen.findByText("账号或密码错误")).toBeInTheDocument();
    expect(screen.getByText("请求编号：req-authentication_failed")).toBeInTheDocument();
    expect(screen.getByLabelText("邮箱")).toHaveValue("lin@example.com");
    expect(screen.getByLabelText("密码")).toHaveValue("");
    // 登录请求带上了打开网站时取得的 CSRF 令牌。
    expect(requests[1]?.headers["x-csrf-token"]).toBe("t1");
    expect(requests[1]?.body).toEqual({ email: "lin@example.com", password: "wrong-password" });
  });

  it("被限速：显示倒计时，倒计时期间按钮不能点", async () => {
    renderApp("/login", [
      json(200, { csrf_token: "t1", authenticated: false }),
      apiError(429, "rate_limited", "尝试次数过多，请稍后再试", { "Retry-After": "30" }),
    ]);

    await submitLogin("lin@example.com", "x");

    const button = await screen.findByRole("button", { name: /秒后再试/ });
    expect(button).toBeDisabled();
    expect(screen.getByText(/尝试次数过多/)).toBeInTheDocument();
  });

  it("登录成功：换上新的 CSRF 令牌，跳回登录前想去的页面", async () => {
    const { router, requests } = renderApp("/login?next=%2F", [
      json(200, { csrf_token: "t1", authenticated: false }),
      json(200, { account: ACCOUNT, csrf_token: "t2" }),
      json(200, { message: "已退出" }),
    ]);

    await submitLogin("lin@example.com", "correct-password");

    expect(await screen.findByRole("heading", { name: "研究空间" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");

    // 退出时用的是登录后换发的新令牌 t2，说明令牌已正确替换。
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "退出登录" }));
    expect(await screen.findByRole("heading", { name: "登录 ChainScope" })).toBeInTheDocument();
    expect(requests[2]?.path).toBe("/api/auth/logout");
    expect(requests[2]?.headers["x-csrf-token"]).toBe("t2");
  });
});

/**
 * 账户设置页的整体测试：用真实的路由表、页面和组件，只把后端换成假后端（fakeBackend.ts）。
 *
 * 覆盖：从侧栏进入设置页；修改密码（前端检查、当前密码不对、限速、成功）；
 * 退出所有设备；注销账户（两次确认、取消、当前密码不对、成功）。
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { routes } from "../src/app/router";
import { AuthProvider } from "../src/features/auth/session/AuthProvider";
import { SessionGate } from "../src/features/auth/session/SessionGate";
import { apiError, installFakeBackend, json, type FakeResponse } from "./fakeBackend";

const ACCOUNT = { user_id: "u-1", email: "lin@example.com", created_at: "2026-10-26T08:00:00Z" };
const LOGGED_IN: FakeResponse[] = [
  json(200, { csrf_token: "t1", authenticated: true }),
  json(200, ACCOUNT),
];
const WRONG_PASSWORD = apiError(401, "authentication_failed", "账号或密码错误");

/** 已登录状态下打开 url（默认设置页）。 */
function renderLoggedIn(responses: FakeResponse[], url = "/settings") {
  const backend = installFakeBackend([...LOGGED_IN, ...responses]);
  const router = createMemoryRouter(routes, { initialEntries: [url] });
  render(
    <AuthProvider>
      <SessionGate>
        <RouterProvider router={router} />
      </SessionGate>
    </AuthProvider>,
  );
  return { router, ...backend, user: userEvent.setup() };
}

/** 按标题找到某个设置分区，后续只在这个分区里查找元素（几个分区都有“当前密码”框）。 */
async function section(title: string) {
  const heading = await screen.findByRole("heading", { name: title });
  return within(heading.closest("section") as HTMLElement);
}

describe("进入设置页", () => {
  it("从左下角“设置”进入：显示四个分区，标签页标题为“设置”", async () => {
    const { router, user } = renderLoggedIn([], "/");

    await user.click(await screen.findByRole("link", { name: "设置" }));

    expect(await screen.findByRole("heading", { name: "设置", level: 1 })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings");
    for (const title of ["账户信息", "修改密码", "退出所有设备", "注销账户"]) {
      expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    }
    expect(document.title).toBe("设置 · ChainScope");
  });
});

describe("修改密码", () => {
  it("前端检查：没填当前密码、新密码太短、新旧密码相同，都不发请求", async () => {
    const { requests, user } = renderLoggedIn([]);
    const form = await section("修改密码");

    await user.click(form.getByRole("button", { name: "修改密码" }));
    expect(form.getByText("请输入当前密码")).toBeInTheDocument();
    expect(form.getByText("请设置密码")).toBeInTheDocument();

    await user.type(form.getByLabelText("当前密码"), "same password 1");
    await user.type(form.getByLabelText("新密码"), "short");
    await user.click(form.getByRole("button", { name: "修改密码" }));
    expect(form.getByText("密码至少 8 个字符")).toBeInTheDocument();

    await user.clear(form.getByLabelText("新密码"));
    await user.type(form.getByLabelText("新密码"), "same password 1");
    await user.click(form.getByRole("button", { name: "修改密码" }));
    expect(form.getByText("新密码不能和当前密码相同")).toBeInTheDocument();

    expect(requests).toHaveLength(2); // 只有打开网站时的两个请求
  });

  it("当前密码不对：提示显示在“当前密码”框下方，清空两个密码框", async () => {
    const { user } = renderLoggedIn([WRONG_PASSWORD]);
    const form = await section("修改密码");

    await user.type(form.getByLabelText("当前密码"), "wrong password");
    await user.type(form.getByLabelText("新密码"), "a new passphrase");
    await user.click(form.getByRole("button", { name: "修改密码" }));

    expect(await form.findByText("当前密码不正确")).toBeInTheDocument();
    expect(form.getByLabelText("当前密码")).toHaveValue("");
    expect(form.getByLabelText("新密码")).toHaveValue("");
  });

  it("输错太多次被限速：按钮显示“请 14 分 30 秒后再试”", async () => {
    const { user } = renderLoggedIn([
      apiError(429, "rate_limited", "尝试次数过多，请稍后再试", { "Retry-After": "870" }),
    ]);
    const form = await section("修改密码");

    await user.type(form.getByLabelText("当前密码"), "wrong password");
    await user.type(form.getByLabelText("新密码"), "a new passphrase");
    await user.click(form.getByRole("button", { name: "修改密码" }));

    expect(await form.findByRole("button", { name: "请 14 分 30 秒后再试" })).toBeDisabled();
  });

  it("成功：请求内容正确，跳到登录页并提示“密码已修改，请用新密码登录”", async () => {
    const { router, requests, user } = renderLoggedIn([json(200, { message: "ok" })]);
    const form = await section("修改密码");

    await user.type(form.getByLabelText("当前密码"), "old passphrase");
    await user.type(form.getByLabelText("新密码"), "a new passphrase");
    await user.click(form.getByRole("button", { name: "修改密码" }));

    expect(await screen.findByText("密码已修改，请用新密码登录")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(requests[2]?.path).toBe("/api/auth/password/change");
    expect(requests[2]?.body).toEqual({
      current_password: "old passphrase",
      new_password: "a new passphrase",
    });
  });
});

describe("退出所有设备", () => {
  it("成功：跳到登录页并提示“已退出所有设备，请重新登录”", async () => {
    const { requests, user } = renderLoggedIn([json(200, { message: "ok" })]);
    const form = await section("退出所有设备");

    await user.type(form.getByLabelText("当前密码"), "my passphrase");
    await user.click(form.getByRole("button", { name: "退出所有设备" }));

    expect(await screen.findByText("已退出所有设备，请重新登录")).toBeInTheDocument();
    expect(requests[2]?.path).toBe("/api/auth/logout-all");
    expect(requests[2]?.body).toEqual({ current_password: "my passphrase" });
  });

  it("没填当前密码：前端提示，不发请求", async () => {
    const { requests, user } = renderLoggedIn([]);
    const form = await section("退出所有设备");

    await user.click(form.getByRole("button", { name: "退出所有设备" }));

    expect(form.getByText("请输入当前密码")).toBeInTheDocument();
    expect(requests).toHaveLength(2);
  });
});

describe("注销账户", () => {
  it("要确认两次：先填密码点“注销账户”只打开确认框；点“取消”关闭，不发请求", async () => {
    const { requests, user } = renderLoggedIn([]);
    const form = await section("注销账户");

    await user.type(form.getByLabelText("当前密码"), "my passphrase");
    await user.click(form.getByRole("button", { name: "注销账户" }));

    const dialog = await screen.findByRole("dialog", { name: "确认注销账户？" });
    expect(within(dialog).getByText(/无法恢复/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "取消" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(requests).toHaveLength(2);
  });

  it("确认后成功：请求内容正确，跳到登录页并提示“账户已注销”", async () => {
    const { router, requests, user } = renderLoggedIn([json(200, { message: "账户已注销" })]);
    const form = await section("注销账户");

    await user.type(form.getByLabelText("当前密码"), "my passphrase");
    await user.click(form.getByRole("button", { name: "注销账户" }));
    const dialog = await screen.findByRole("dialog", { name: "确认注销账户？" });
    await user.click(within(dialog).getByRole("button", { name: "确认注销" }));

    expect(await screen.findByText("账户已注销，相关数据已删除")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(requests[2]?.path).toBe("/api/auth/account/delete");
    expect(requests[2]?.body).toEqual({ current_password: "my passphrase" });
  });

  it("确认后当前密码不对：关闭确认框，提示显示在密码框下方，账户保留", async () => {
    const { router, user } = renderLoggedIn([WRONG_PASSWORD]);
    const form = await section("注销账户");

    await user.type(form.getByLabelText("当前密码"), "wrong password");
    await user.click(form.getByRole("button", { name: "注销账户" }));
    const dialog = await screen.findByRole("dialog", { name: "确认注销账户？" });
    await user.click(within(dialog).getByRole("button", { name: "确认注销" }));

    expect(await form.findByText("当前密码不正确")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings");
  });

  it("没填当前密码：前端提示，不打开确认框", async () => {
    const { user } = renderLoggedIn([]);
    const form = await section("注销账户");

    await user.click(form.getByRole("button", { name: "注销账户" }));

    expect(form.getByText("请输入当前密码")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

/**
 * 忘记密码流程的整体测试：用真实的路由表、页面和通用三步流程组件，只把后端换成假后端。
 *
 * 覆盖：登录页“忘记密码？”带上已填的邮箱；三步走完后回到登录页并提示“密码已重置”；
 * 请求内容与后端接口一致；重置凭证失效时提示重新开始；文字使用“新密码”。
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { routes } from "../src/app/router";
import { AuthProvider } from "../src/features/auth/session/AuthProvider";
import { SessionGate } from "../src/features/auth/session/SessionGate";
import { apiError, installFakeBackend, json, type FakeResponse } from "./fakeBackend";

const ANONYMOUS = json(200, { csrf_token: "t1", authenticated: false });
const NOTICE = "如果这个邮箱有账户，验证码已经发出，请查收（也请检查垃圾邮件）";

/** 从登录页开始，起始时未登录。 */
function renderFromLogin(responses: FakeResponse[]) {
  const backend = installFakeBackend([ANONYMOUS, ...responses]);
  const router = createMemoryRouter(routes, { initialEntries: ["/login"] });
  render(
    <AuthProvider>
      <SessionGate>
        <RouterProvider router={router} />
      </SessionGate>
    </AuthProvider>,
  );
  return { router, ...backend, user: userEvent.setup() };
}

describe("忘记密码", () => {
  it("登录页点“忘记密码？”：带上已填的邮箱；三步完成后回到登录页，提示密码已重置", async () => {
    const { router, requests, user } = renderFromLogin([
      json(202, { challenge_id: "r-1", message: NOTICE }),
      json(200, { reset_token: "reset-token" }),
      json(200, { message: "密码已重置，所有设备已退出登录，请用新密码登录" }),
    ]);

    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.click(screen.getByRole("link", { name: "忘记密码？" }));

    // 重置页：标题、邮箱已带过来
    expect(await screen.findByRole("heading", { name: "重置密码" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/reset-password");
    expect(screen.getByLabelText("邮箱")).toHaveValue("lin@example.com");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));

    // 第 2 步：中性的说明，加上后端的提示
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    expect(screen.getByText(/的收件箱/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("验证码"), "654321");
    await user.click(screen.getByRole("button", { name: "下一步" }));

    // 第 3 步：文字是“新密码”
    // 说明句被邮箱的 <span> 分成了几段，按整个段落的完整文字匹配
    // （步骤条里也有“设置新密码”四个字，所以要匹配完整的一句）。
    expect(
      await screen.findByText(
        (_, element) =>
          element?.tagName === "P" && element.textContent === "正在为 lin@example.com 设置新密码",
      ),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("新密码"), "a brand new passphrase");
    await user.click(screen.getByRole("button", { name: "重置密码" }));

    // 回到登录页：提示密码已重置，邮箱已填好
    expect(
      await screen.findByText("密码已重置，所有设备已退出，请用新密码登录"),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(screen.getByLabelText("邮箱")).toHaveValue("lin@example.com");

    expect(requests.slice(1).map((r) => [r.path, r.body])).toEqual([
      ["/api/auth/password-reset/request", { email: "lin@example.com" }],
      ["/api/auth/password-reset/verify", { challenge_id: "r-1", code: "654321" }],
      [
        "/api/auth/password-reset/complete",
        { reset_token: "reset-token", new_password: "a brand new passphrase" },
      ],
    ]);
  });

  it("登录页邮箱没填时点“忘记密码？”：重置页邮箱为空，光标在邮箱框", async () => {
    const { user } = renderFromLogin([]);

    await user.click(await screen.findByRole("link", { name: "忘记密码？" }));

    const email = await screen.findByLabelText("邮箱");
    expect(email).toHaveValue("");
    expect(email).toHaveFocus();
  });

  it("重置凭证失效：提示重新获取，点“重新开始”回到第 1 步并保留邮箱", async () => {
    const { user } = renderFromLogin([
      json(202, { challenge_id: "r-1", message: NOTICE }),
      json(200, { reset_token: "reset-token" }),
      apiError(400, "password_reset_failed", "无法完成密码重置，请重新获取验证码"),
    ]);

    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.click(screen.getByRole("link", { name: "忘记密码？" }));
    await user.click(await screen.findByRole("button", { name: "获取验证码" }));
    await user.type(await screen.findByLabelText("验证码"), "654321");
    await user.click(screen.getByRole("button", { name: "下一步" }));
    await user.type(await screen.findByLabelText("新密码"), "a brand new passphrase");
    await user.click(screen.getByRole("button", { name: "重置密码" }));

    expect(await screen.findByText("无法完成密码重置，请重新获取验证码")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新开始" }));
    expect(await screen.findByLabelText("邮箱")).toHaveValue("lin@example.com");
  });

  it("“返回登录”链接回到登录页", async () => {
    const { router, user } = renderFromLogin([]);

    await user.click(await screen.findByRole("link", { name: "忘记密码？" }));
    await user.click(await screen.findByRole("link", { name: "返回登录" }));

    expect(await screen.findByRole("heading", { name: "登录 ChainScope" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });
});

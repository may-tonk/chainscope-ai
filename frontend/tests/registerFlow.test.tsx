/**
 * 注册流程的整体测试：用真实的路由表、页面和步骤组件，只把后端换成假后端（fakeBackend.ts）。
 *
 * 覆盖：三步走完后跳到登录页并带上邮箱；前端格式检查不发请求；
 * 验证码输错、重新发送、密码被后端拒绝、流程失效后重新开始等情况的显示。
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
const NOTICE = "如果这个邮箱可以注册，验证码已经发出，请查收（也请检查垃圾邮件）";
const challenge = (id: string) => json(202, { challenge_id: id, message: NOTICE });

/** 打开 /register，起始时未登录。 */
function renderRegister(responses: FakeResponse[]) {
  const backend = installFakeBackend([ANONYMOUS, ...responses]);
  const router = createMemoryRouter(routes, { initialEntries: ["/register"] });
  render(
    <AuthProvider>
      <SessionGate>
        <RouterProvider router={router} />
      </SessionGate>
    </AuthProvider>,
  );
  return { router, ...backend, user: userEvent.setup() };
}

describe("注册流程", () => {
  it("三步走完：跳到登录页，邮箱已填好，显示“注册成功，请登录”", async () => {
    const { router, requests, user } = renderRegister([
      challenge("c-1"),
      json(200, { registration_token: "reg-token" }),
      json(201, { message: "注册成功，请登录" }),
    ]);

    // 第 1 步：邮箱
    await user.type(await screen.findByLabelText("邮箱"), " lin@example.com ");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));

    // 第 2 步：验证码（粘贴带空格的内容会自动整理）
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    expect(screen.getByText("lin@example.com")).toBeInTheDocument();
    await user.type(screen.getByLabelText("验证码"), "123 456");
    expect(screen.getByLabelText("验证码")).toHaveValue("123456");
    await user.click(screen.getByRole("button", { name: "下一步" }));

    // 第 3 步：密码
    const password = await screen.findByLabelText("密码");
    await user.type(password, "a good passphrase");
    expect(screen.getByText("已输入 17 个字符（需要 8～128 个）")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "完成注册" }));

    // 跳到登录页：邮箱已填好，提示注册成功
    expect(await screen.findByText("注册成功，请登录")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(screen.getByLabelText("邮箱")).toHaveValue("lin@example.com");

    // 三个请求的内容：邮箱已去掉首尾空格；验证码带上第 1 步的编号；第 3 步带上凭证
    expect(requests.slice(1).map((r) => [r.path, r.body])).toEqual([
      ["/api/auth/registration/request", { email: "lin@example.com" }],
      ["/api/auth/registration/verify", { challenge_id: "c-1", code: "123456" }],
      [
        "/api/auth/registration/complete",
        { registration_token: "reg-token", password: "a good passphrase" },
      ],
    ]);
  });

  it("邮箱格式不对：输入框下方提示，不发请求", async () => {
    const { requests, user } = renderRegister([]);

    await user.type(await screen.findByLabelText("邮箱"), "not-an-email");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));

    expect(screen.getByText("邮箱格式不正确")).toBeInTheDocument();
    expect(requests).toHaveLength(1); // 只有打开网站时那一次
  });

  it("发得太频繁：显示倒计时，按钮不能点", async () => {
    const { user } = renderRegister([
      apiError(429, "rate_limited", "尝试次数过多，请稍后再试", { "Retry-After": "45" }),
    ]);

    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));

    expect(await screen.findByRole("button", { name: /秒后再试/ })).toBeDisabled();
  });

  it("验证码输错：提示显示在输入框下方，清空输入框", async () => {
    const { user } = renderRegister([
      challenge("c-1"),
      apiError(400, "verification_failed", "验证码错误或已失效，请重新获取"),
    ]);

    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));
    await user.type(await screen.findByLabelText("验证码"), "000000");
    await user.click(screen.getByRole("button", { name: "下一步" }));

    expect(await screen.findByText("验证码错误或已失效，请重新获取")).toBeInTheDocument();
    expect(screen.getByLabelText("验证码")).toHaveValue("");
    expect(screen.getByLabelText("验证码")).toHaveAttribute("aria-invalid", "true");
  });

  it("验证码不足 6 位：前端提示，不发请求", async () => {
    const { requests, user } = renderRegister([challenge("c-1")]);

    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));
    await user.type(await screen.findByLabelText("验证码"), "123");
    await user.click(screen.getByRole("button", { name: "下一步" }));

    expect(screen.getByText("验证码是 6 位数字")).toBeInTheDocument();
    expect(requests).toHaveLength(2);
  });

  it("刚发出验证码时，“重新发送”要先等 60 秒；“修改邮箱”回到第 1 步并保留邮箱", async () => {
    const { user } = renderRegister([challenge("c-1")]);

    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));

    // 刚开始倒数时显示“1 分钟后可重新发送”，随后变成“59 秒后可重新发送”……（见 formatDuration）。
    expect(await screen.findByRole("button", { name: /后可重新发送/ })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "修改邮箱" }));
    expect(await screen.findByLabelText("邮箱")).toHaveValue("lin@example.com");
  });

  it("密码太短：前端提示，不发请求；后端判定太常见：提示显示在密码框下方", async () => {
    const { requests, user } = renderRegister([
      challenge("c-1"),
      json(200, { registration_token: "reg-token" }),
      apiError(400, "password_common", "这个密码太常见或太容易猜到，请换一个"),
    ]);

    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));
    await user.type(await screen.findByLabelText("验证码"), "123456");
    await user.click(screen.getByRole("button", { name: "下一步" }));

    const password = await screen.findByLabelText("密码");
    await user.type(password, "short");
    await user.click(screen.getByRole("button", { name: "完成注册" }));
    expect(screen.getByText("密码至少 8 个字符")).toBeInTheDocument();
    expect(requests).toHaveLength(3);

    await user.clear(password);
    await user.type(password, "password123");
    await user.click(screen.getByRole("button", { name: "完成注册" }));
    expect(await screen.findByText("这个密码太常见或太容易猜到，请换一个")).toBeInTheDocument();
  });

  it("注册凭证失效：提示重新开始，点“重新开始”回到第 1 步", async () => {
    const { user } = renderRegister([
      challenge("c-1"),
      json(200, { registration_token: "reg-token" }),
      apiError(400, "registration_failed", "无法完成注册，请重新开始注册流程"),
    ]);

    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));
    await user.type(await screen.findByLabelText("验证码"), "123456");
    await user.click(screen.getByRole("button", { name: "下一步" }));
    await user.type(await screen.findByLabelText("密码"), "a good passphrase");
    await user.click(screen.getByRole("button", { name: "完成注册" }));

    expect(await screen.findByText("无法完成注册，请重新开始注册流程")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新开始" }));
    expect(await screen.findByLabelText("邮箱")).toHaveValue("lin@example.com");
  });
});

describe("密码框的眼睛图标", () => {
  it("默认隐藏密码；点击后显示，再点隐藏（按钮名称说明点了会怎样）", async () => {
    const { user } = renderRegister([
      challenge("c-1"),
      json(200, { registration_token: "reg-token" }),
    ]);
    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.click(screen.getByRole("button", { name: "获取验证码" }));
    await user.type(await screen.findByLabelText("验证码"), "123456");
    await user.click(screen.getByRole("button", { name: "下一步" }));

    const password = await screen.findByLabelText("密码");
    expect(password).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "显示密码" }));
    expect(password).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "隐藏密码" }));
    expect(password).toHaveAttribute("type", "password");
  });
});

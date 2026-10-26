/**
 * 等待时间的显示（src/shared/ui/useCountdown.ts 的 formatDuration）测试，
 * 以及长时间限速时，登录页按钮上显示的是“分 秒”而不是一大串秒数。
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { routes } from "../src/app/router";
import { AuthProvider } from "../src/features/auth/session/AuthProvider";
import { SessionGate } from "../src/features/auth/session/SessionGate";
import { formatDuration } from "../src/shared/ui/useCountdown";
import { apiError, installFakeBackend, json } from "./fakeBackend";

describe("formatDuration", () => {
  it.each([
    [0, "0 秒"],
    [1, "1 秒"],
    [59, "59 秒"],
    [60, "1 分钟"],
    [61, "1 分 1 秒"],
    [900, "15 分钟"],
    [1525, "25 分 25 秒"],
    [3600, "60 分钟"],
  ])("%i 秒 → %s", (seconds, text) => {
    expect(formatDuration(seconds)).toBe(text);
  });
});

describe("长时间限速的显示", () => {
  it("后端要求等 1525 秒：按钮显示“请 25 分 25 秒后再试”", async () => {
    installFakeBackend([
      json(200, { csrf_token: "t1", authenticated: false }),
      apiError(429, "rate_limited", "尝试次数过多，请稍后再试", { "Retry-After": "1525" }),
    ]);
    const router = createMemoryRouter(routes, { initialEntries: ["/login"] });
    render(
      <AuthProvider>
        <SessionGate>
          <RouterProvider router={router} />
        </SessionGate>
      </AuthProvider>,
    );
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText("邮箱"), "lin@example.com");
    await user.type(screen.getByLabelText("密码"), "x");
    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByRole("button", { name: "请 25 分 25 秒后再试" })).toBeDisabled();
    expect(screen.getByText(/（25 分 25 秒后可以再试）/)).toBeInTheDocument();
  });
});

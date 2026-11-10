/** 实际路由集成：登录保护、首页草稿传递、账户隔离及会话导航。 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { routes } from "../src/app/router";
import { AuthContext, type AuthContextValue } from "../src/features/auth/session/AuthContext";

/** 测试专用身份上下文，不创建真实账户或改动网站认证逻辑。 */
function identity(id: string): AuthContextValue {
  return { state: { status: "authenticated", account: { user_id: id, email: `${id}@example.com`, created_at: "2026-10-25T00:00:00Z" } }, login: vi.fn(), logout: vi.fn(), reload: vi.fn(), endSession: vi.fn() };
}

describe("研究会话路由", () => {
  it.each(["/research/new", "/research/session-a"])("未登录不能访问 %s", async (path) => {
    const router = createMemoryRouter(routes, { initialEntries: [path] });
    render(<AuthContext.Provider value={{ ...identity("a"), state: { status: "anonymous" } }}><RouterProvider router={router} /></AuthContext.Provider>);
    expect(await screen.findByRole("heading", { name: "登录 ChainScope" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(new URLSearchParams(router.state.location.search).get("next")).toBe(path);
  });

  it("首页进入会话携带问题及完整对象，不伪造已发送消息", async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(routes, { initialEntries: ["/"] });
    render(<AuthContext.Provider value={identity("a")}><RouterProvider router={router} /></AuthContext.Provider>);
    await user.click(screen.getByRole("button", { name: /BEM 的活动发生了什么变化/ }));
    await user.click(screen.getByRole("button", { name: "进入研究会话" }));
    expect(await screen.findByRole("heading", { name: "新研究会话" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/research/new");
    expect(screen.getByRole("textbox", { name: "继续你的研究" })).toHaveValue("帮我分析 BEM 最近的链上活动变化，区分普通转账与零地址发出事件。");
    expect(screen.getByTitle("0x5ce033b2bfca3af30b3e8c8457deaf776a8b695a")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "研究消息" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });

  it("忽略其他账户留在 history state 中的私人草稿", () => {
    const router = createMemoryRouter(routes, { initialEntries: [{ pathname: "/research/new", state: { owner: "a", draft: { question: "账户 A 的私人问题", contexts: [] } } }] });
    render(<AuthContext.Provider value={identity("b")}><RouterProvider router={router} /></AuthContext.Provider>);
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(screen.queryByText("账户 A 的私人问题")).not.toBeInTheDocument();
  });

  it("同一路由切换账户会清理本地草稿和弹窗", async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(routes, { initialEntries: ["/research/new"] });
    const { rerender } = render(<AuthContext.Provider value={identity("a")}><RouterProvider router={router} /></AuthContext.Provider>);
    await user.type(screen.getByRole("textbox"), "私人草稿");
    await user.click(screen.getByRole("button", { name: "补充时间（按需）" }));
    rerender(<AuthContext.Provider value={identity("b")}><RouterProvider router={router} /></AuthContext.Provider>);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("textbox")).toHaveValue("");
  });
});

/** 实际页面路由验证：首次创建替换 URL 后保持流，刷新 URL 能恢复历史。 */
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../src/features/memory/api";
import { ChatRoute } from "../src/features/memory/PersistentChatRoute";
import type { ChatEvent } from "../src/features/research/contracts/session";

vi.mock("../src/features/auth/session/AuthContext", () => ({ useAuth: () => ({ state: { status: "authenticated", account: { user_id: "owner" } } }) }));
vi.mock("../src/features/memory/api", () => ({ listConversations: vi.fn(), createConversation: vi.fn(), getSnapshot: vi.fn(), streamTurn: vi.fn(), stopTurn: vi.fn(), deleteConversation: vi.fn() }));

const row = { conversation_id: "saved-conversation", title: "已保存对话", revision: 1, updated_at: "2026-11-17T00:00:00Z" };
const routes = [{ path: "/research/new", element: <ChatRoute /> }, { path: "/chat/:conversationId", element: <ChatRoute /> }];

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listConversations).mockResolvedValue([]);
  vi.mocked(api.createConversation).mockResolvedValue(row);
  vi.mocked(api.getSnapshot).mockResolvedValue({ conversation: row, messages: [], next_sequence: 0, has_more: false, poll_after_ms: 60000 });
});
afterEach(cleanup);

it("首轮替换 URL 后保持真实页面流且能显示最终回复", async () => {
  let receive: (event: ChatEvent) => void = () => undefined;
  let finish: () => void = () => undefined;
  let signal: AbortSignal | undefined;
  vi.mocked(api.streamTurn).mockImplementation((_id, _revision, _key, _text, nextSignal, callback) => {
    signal = nextSignal; receive = callback;
    return new Promise<void>((resolve, reject) => { finish = resolve; nextSignal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }); });
  });
  const router = createMemoryRouter(routes, { initialEntries: ["/research/new"] });
  render(<RouterProvider router={router} />);
  await userEvent.type(screen.getByPlaceholderText("提出问题，或粘贴地址、合约、交易哈希…"), "解释日志{Enter}");
  await waitFor(() => expect(api.streamTurn).toHaveBeenCalledOnce());
  expect(router.state.location.pathname).toBe(`/chat/${row.conversation_id}`);
  expect(signal?.aborted).toBe(false);
  act(() => {
    receive({ session_id: row.conversation_id, turn_id: "server-turn", request_revision: 2, sequence: 1, payload: { type: "turn.started" } });
    receive({ session_id: row.conversation_id, turn_id: "server-turn", request_revision: 2, sequence: 2, payload: { type: "message.delta", text: "持久正文" } });
    receive({ session_id: row.conversation_id, turn_id: "server-turn", request_revision: 2, sequence: 3, payload: { type: "turn.completed", text: "持久正文" } });
    finish();
  });
  await screen.findByText("持久正文");
  expect(screen.getByText("对话保存到你的账户 · 离页停止生成")).toBeInTheDocument();
});

it("直接打开持久 URL 从服务端恢复原文且不请求模型", async () => {
  vi.mocked(api.getSnapshot).mockResolvedValue({ conversation: row, messages: [
    { message_id: "m1", conversation_id: row.conversation_id, turn_id: "t1", sequence: 1, role: "user", status: "completed", content: "历史问题" },
    { message_id: "m2", conversation_id: row.conversation_id, turn_id: "t1", sequence: 2, role: "assistant", status: "stopped", content: "保存的半截回复" },
  ], next_sequence: 2, has_more: false, poll_after_ms: 60000 });
  const router = createMemoryRouter(routes, { initialEntries: [`/chat/${row.conversation_id}`] });
  render(<RouterProvider router={router} />);
  await screen.findByText("保存的半截回复");
  expect(api.streamTurn).not.toHaveBeenCalled();
  expect(screen.getByText("历史问题")).toBeInTheDocument();
});

/** 持久聊天控制器验证：恢复、URL 接续、网络幂等和会话切换使用明确 API 替身。 */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../src/shared/api/client";
import * as api from "../src/features/memory/api";
import { usePersistentChat } from "../src/features/memory/usePersistentChat";
import type { ChatEvent } from "../src/features/research/contracts/session";

vi.mock("../src/features/memory/api", () => ({ createConversation: vi.fn(), getSnapshot: vi.fn(), streamTurn: vi.fn(), stopTurn: vi.fn() }));

const row = { conversation_id: "conversation-a", title: "历史对话", revision: 1, updated_at: "2026-11-17T00:00:00Z" };

/** 虚构持久记录，展示状态与真实 API 响应形状相同。 */
function snapshot(id = row.conversation_id, text = "已保存的回答"): api.Snapshot {
  return { conversation: { ...row, conversation_id: id }, messages: [
    { message_id: "m1", conversation_id: id, turn_id: "turn-1", sequence: 1, role: "user", status: "completed", content: "之前的问题" },
    { message_id: "m2", conversation_id: id, turn_id: "turn-1", sequence: 2, role: "assistant", status: "completed", content: text },
  ], next_sequence: 2, has_more: false, poll_after_ms: 60000 };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.createConversation).mockResolvedValue(row);
  vi.mocked(api.getSnapshot).mockResolvedValue({ ...snapshot(), messages: [], next_sequence: 0 });
  vi.mocked(api.stopTurn).mockResolvedValue();
});
afterEach(cleanup);

describe("持久聊天", () => {
  it("刷新路由从服务端恢复消息和修订", async () => {
    vi.mocked(api.getSnapshot).mockResolvedValue(snapshot());
    const { result } = renderHook(() => usePersistentChat(row.conversation_id, "initial", vi.fn(), vi.fn()));
    await waitFor(() => expect(result.current.turns[0]?.answer).toBe("已保存的回答"));
    expect(result.current.conversation?.conversation_id).toBe(row.conversation_id);
    expect(api.streamTurn).not.toHaveBeenCalled();
  });

  it("首轮创建后更新 URL 不取消正在进行的流，发送只包含本轮参数", async () => {
    const created = vi.fn();
    let receive: (event: ChatEvent) => void = () => undefined;
    let finish: () => void = () => undefined;
    let signal: AbortSignal | undefined;
    vi.mocked(api.streamTurn).mockImplementation((_id, _revision, _key, _content, nextSignal, callback) => {
      signal = nextSignal; receive = callback;
      return new Promise<void>((resolve, reject) => { finish = resolve; nextSignal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }); });
    });
    const { result, rerender } = renderHook(({ id, key }) => usePersistentChat(id, key, created, vi.fn()), { initialProps: { id: undefined as string | undefined, key: "new" } });
    act(() => result.current.send("新的问题"));
    await waitFor(() => expect(api.streamTurn).toHaveBeenCalledOnce());
    expect(created).toHaveBeenCalledWith(row.conversation_id);
    expect(vi.mocked(api.streamTurn).mock.calls[0]?.slice(0, 4)).toEqual([row.conversation_id, 1, expect.any(String), "新的问题"]);
    rerender({ id: row.conversation_id, key: "replaced-url" });
    expect(signal?.aborted).toBe(false);
    act(() => {
      receive({ session_id: row.conversation_id, turn_id: "server-turn", request_revision: 2, sequence: 1, payload: { type: "turn.started" } });
      receive({ session_id: row.conversation_id, turn_id: "server-turn", request_revision: 2, sequence: 2, payload: { type: "message.delta", text: "实时正文" } });
      receive({ session_id: row.conversation_id, turn_id: "server-turn", request_revision: 2, sequence: 3, payload: { type: "turn.completed", text: "实时正文" } });
      finish();
    });
    await waitFor(() => expect(result.current.turns[0]?.status).toBe("complete"));
    expect(result.current.conversation?.revision).toBe(2);
  });

  it("网络结果不确定时重试复用幂等键和原修订，不重复显示用户消息", async () => {
    vi.mocked(api.streamTurn).mockRejectedValue(new ApiError(0, "network_error", "连接中断"));
    const { result } = renderHook(() => usePersistentChat(undefined, "new", vi.fn(), vi.fn()));
    act(() => result.current.send("问题"));
    await waitFor(() => expect(result.current.turns[0]?.status).toBe("failed"));
    const first = vi.mocked(api.streamTurn).mock.calls[0]?.slice(0, 4);
    vi.mocked(api.getSnapshot).mockResolvedValue({ ...snapshot(), conversation: { ...row, revision: 2 } });
    act(() => result.current.retry());
    await waitFor(() => expect(api.streamTurn).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.streamTurn).mock.calls[1]?.slice(0, 4)).toEqual(first);
    expect(result.current.turns).toHaveLength(1);
  });

  it("切换会话后旧历史响应不能覆盖新会话", async () => {
    let resolveOld: (value: api.Snapshot) => void = () => undefined;
    vi.mocked(api.getSnapshot).mockImplementation((id) => id === "old" ? new Promise((resolve) => { resolveOld = resolve; }) : Promise.resolve(snapshot(id, "新的会话")));
    const { result, rerender } = renderHook(({ id }) => usePersistentChat(id, id, vi.fn(), vi.fn()), { initialProps: { id: "old" } });
    rerender({ id: "new" });
    await waitFor(() => expect(result.current.turns[0]?.answer).toBe("新的会话"));
    await act(async () => resolveOld(snapshot("old", "旧的私人内容")));
    expect(result.current.turns[0]?.answer).toBe("新的会话");
  });
});

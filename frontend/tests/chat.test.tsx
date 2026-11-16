/** 聊天状态和页面集成：测试网络替身不进入业务代码，覆盖真实流增量和用户操作。 */
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { setCsrfToken } from "../src/shared/api/client";
import type {
  ChatEvent,
  ChatInput,
  ChatPayload,
} from "../src/features/research/contracts/session";
import { useChat } from "../src/features/research/hooks/useChat";
import { ChatPage } from "../src/features/research/pages/ChatPage";

/** 一轮可人工推进的测试传输；信号与回调都保留以检验取消后的晚到事件。 */
function controlled() {
  const calls: {
    input: ChatInput;
    signal: AbortSignal;
    receive: (event: ChatEvent) => void;
    finish: () => void;
    reject: (error: Error) => void;
  }[] = [];
  const transport = vi.fn(
    (
      input: ChatInput,
      signal: AbortSignal,
      receive: (event: ChatEvent) => void
    ) =>
      new Promise<void>((finish, reject) =>
        calls.push({ input, signal, receive, finish, reject })
      )
  );
  /** 明确断言目标请求已发起，避免用非空断言掩盖请求没有产生的失败。 */
  function call(index: number) {
    const value = calls[index];
    if (!value) throw new Error(`测试请求 ${index} 尚未产生`);
    return value;
  }
  return { calls, transport, call };
}

/** Hook 测试只关心事件显示行为；序号及字节边界另由 chatStream.test 验证。 */
function event(payload: ChatPayload): ChatEvent {
  return { sequence: 1, session_id: "test", turn_id: "test", payload };
}

describe("当前页聊天控制", () => {
  it("完成终态先于连接关闭时，立即追问仍保留已完成上下文", () => {
    const c = controlled();
    const { result } = renderHook(() => useChat(c.transport));
    act(() => result.current.send("第一问"));
    // 故意不结束 transport，模拟终态数据已到而 HTTP 还在收尾。
    act(() =>
      c.call(0).receive(event({ type: "turn.completed", text: "完整回答" }))
    );
    act(() => result.current.send("立即追问"));
    expect(c.call(0).signal.aborted).toBe(true);
    expect(result.current.turns[0]?.status).toBe("complete");
    expect(c.call(1).input.messages).toContainEqual({
      role: "assistant",
      content: "完整回答",
    });
  });

  it("失败终态先于连接关闭时，重试按钮立即生效且不重复问题", () => {
    const c = controlled();
    const { result } = renderHook(() => useChat(c.transport));
    act(() => result.current.send("第一问"));
    act(() =>
      c
        .call(0)
        .receive(
          event({ type: "turn.failed", code: "TEST", message: "测试故障" })
        )
    );
    act(() => result.current.retry());
    expect(c.call(0).signal.aborted).toBe(true);
    expect(c.call(1).input).toEqual(c.call(0).input);
    expect(result.current.turns).toHaveLength(1);
    expect(result.current.turns[0]?.status).toBe("waiting");
  });

  it("流式更新后继续提问携带已完成上下文", async () => {
    const c = controlled();
    const { result } = renderHook(() => useChat(c.transport));
    act(() => result.current.send("第一问"));
    act(() =>
      c.call(0).receive(event({ type: "message.delta", text: "部分" }))
    );
    expect(result.current.turns[0]?.answer).toBe("部分");
    await act(async () => {
      c.call(0).receive(event({ type: "turn.completed", text: "完整回答" }));
      c.call(0).finish();
    });
    act(() => result.current.send("继续问"));
    expect(c.call(1).input.messages).toEqual([
      { role: "user", content: "第一问" },
      { role: "assistant", content: "完整回答" },
      { role: "user", content: "继续问" },
    ]);
    expect(result.current.turns[0]?.answer).toBe("完整回答");
  });

  it("新问题停止旧请求，旧轮晚到的分片和完成都不能覆盖新问题", async () => {
    const c = controlled();
    const { result } = renderHook(() => useChat(c.transport));
    act(() => result.current.send("旧问题"));
    act(() =>
      c.call(0).receive(event({ type: "message.delta", text: "保留的部分" }))
    );
    act(() => result.current.send("新问题"));
    expect(c.call(0).signal.aborted).toBe(true);
    await act(async () => {
      c.call(0).receive(event({ type: "turn.completed", text: "过期结果" }));
      c.call(0).finish();
    });
    expect(result.current.turns[0]).toMatchObject({
      status: "stopped",
      answer: "保留的部分",
    });
    expect(result.current.turns[1]).toMatchObject({
      status: "waiting",
      answer: "",
    });
    expect(
      c
        .call(1)
        .input.messages.some((message) => message.content === "保留的部分")
    ).toBe(false);
  });

  it("停止与卸载取消连接；错误重试不重复插入用户问题", async () => {
    const c = controlled();
    const { result, unmount } = renderHook(() => useChat(c.transport));
    act(() => result.current.send("重试问题"));
    await act(async () =>
      c.call(0).reject(new Error("private provider detail"))
    );
    expect(result.current.turns[0]?.error).not.toContain("private");
    act(() => result.current.retry());
    expect(c.call(1).input).toEqual(c.call(0).input);
    expect(result.current.turns).toHaveLength(1);
    act(() => result.current.stop());
    expect(c.call(1).signal.aborted).toBe(true);
    act(() => result.current.send("最后的问题"));
    unmount();
    expect(c.call(2).signal.aborted).toBe(true);
  });
});

describe("真实请求适配的聊天页面", () => {
  it("聊天快捷键保留换行与中文候选确认，普通 Enter 才发送", async () => {
    const user = userEvent.setup();
    setCsrfToken("test-csrf");
    const fetch = vi.fn().mockResolvedValue(
      new Response(new ReadableStream(), {
        headers: { "Content-Type": "text/event-stream" },
      })
    );
    vi.stubGlobal("fetch", fetch);
    render(
      <MemoryRouter>
        <ChatPage />
      </MemoryRouter>
    );
    const input = screen.getByRole("textbox", { name: "继续你的研究" });
    await user.type(input, "解释链上日志");
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    expect(input).toHaveValue("解释链上日志\n");
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(fetch).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(input).toHaveValue("");
    expect(
      screen.getByRole("button", { name: "停止回复" })
    ).toBeInTheDocument();
  });

  it("向上阅读时新分片不拉回底部，用户可主动跳到最新回复", async () => {
    const user = userEvent.setup();
    setCsrfToken("test-csrf");
    let source!: ReadableStreamDefaultController<Uint8Array>;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(controller) {
              source = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } }
        )
      )
    );
    render(
      <MemoryRouter>
        <ChatPage />
      </MemoryRouter>
    );
    await user.type(screen.getByRole("textbox"), "说明链上日志");
    await user.click(screen.getByRole("button", { name: "发送" }));
    const region = screen.getByRole("region", { name: "聊天消息区" });
    // jsdom 不进行排版，这里只提供“消息超出视口”的测量值，几何布局另用真实浏览器验收。
    Object.defineProperties(region, {
      scrollHeight: { value: 1000 },
      clientHeight: { value: 200 },
    });
    region.scrollTop = 0;
    fireEvent.scroll(region);
    expect(
      screen.getByRole("button", { name: "查看最新回复" })
    ).toBeInTheDocument();
    await act(async () => {
      for (const [sequence, payload] of [
        [1, { type: "turn.started" }],
        [2, { type: "message.delta", text: "新内容" }],
      ] as const) {
        source.enqueue(
          new TextEncoder().encode(
            `data: ${JSON.stringify({
              session_id: "test",
              turn_id: "test",
              sequence,
              payload,
            })}\n\n`
          )
        );
      }
    });
    expect(region.scrollTop).toBe(0);
    await user.click(screen.getByRole("button", { name: "查看最新回复" }));
    expect(region.scrollTop).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: "查看最新回复" })
    ).not.toBeInTheDocument();
  });

  it("发送后在完成前显示分片，保留中文输入和后续追问能力", async () => {
    const user = userEvent.setup();
    setCsrfToken("test-csrf");
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(value) {
              controller = value;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } }
        )
      )
    );
    render(
      <MemoryRouter>
        <ChatPage />
      </MemoryRouter>
    );
    await user.type(
      screen.getByRole("textbox", { name: "继续你的研究" }),
      "什么是链上日志？"
    );
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(
      await screen.findByRole("button", { name: "停止回复" })
    ).toBeInTheDocument();
    /** 向测试网络流写入完整帧，组件必须立即渲染，而非等整个响应结束。 */
    const write = (sequence: number, payload: ChatPayload) =>
      controller.enqueue(
        new TextEncoder().encode(
          `data: ${JSON.stringify({
            session_id: "test",
            turn_id: "test",
            sequence,
            payload,
          })}\n\n`
        )
      );
    await act(async () => {
      write(1, { type: "turn.started" });
      write(2, { type: "message.delta", text: "日志记录合约事件" });
    });
    expect(await screen.findByText("日志记录合约事件")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeEnabled();
    await act(async () => {
      write(3, { type: "turn.completed", text: "日志记录合约事件。" });
      controller.close();
    });
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("回复完成")
    );
    expect(
      screen.getByRole("list", { name: "研究消息" }).children
    ).toHaveLength(2);
    expect(
      screen.queryByRole("button", { name: "停止回复" })
    ).not.toBeInTheDocument();
  });
});

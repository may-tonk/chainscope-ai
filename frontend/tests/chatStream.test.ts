/** SSE 传输测试：真实解析器和共用请求层，网络字节流由测试生成。 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { onSessionExpired, setCsrfToken } from "../src/shared/api/client";
import { parseChatEvent, streamChat } from "../src/features/research/api/chat";
import type {
  ChatEvent,
  ChatPayload,
} from "../src/features/research/contracts/session";

/** 编造明确标为测试的帧，不代表供应商实际回答。 */
function frame(sequence: number, payload: ChatPayload, turn = "test-turn") {
  return `data: ${JSON.stringify({
    session_id: "test-session",
    turn_id: turn,
    sequence,
    payload,
  })}\r\n\r\n`;
}

/** 构造任意分片大小的 UTF-8 SSE，检验多字节字符跨网络块的处理。 */
function response(text: string, chunkSize = 7) {
  const bytes = new TextEncoder().encode(text);
  return new Response(
    new ReadableStream({
      start(controller) {
        for (let index = 0; index < bytes.length; index += chunkSize)
          controller.enqueue(bytes.slice(index, index + chunkSize));
        controller.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream; charset=utf-8" } }
  );
}

afterEach(() => setCsrfToken(null));

describe("主 LLM SSE", () => {
  it("逐帧交付中文，忽略心跳，HTTP 请求沿用 CSRF 与同源 Cookie", async () => {
    setCsrfToken("test-csrf");
    const fetch = vi
      .fn()
      .mockResolvedValue(
        response(
          ": heartbeat\r\n\r\n" +
            frame(1, { type: "turn.started" }) +
            frame(2, { type: "message.delta", text: "中文" }) +
            frame(3, { type: "message.delta", text: "回复" }) +
            frame(4, { type: "turn.completed", text: "中文回复" }),
          1
        )
      );
    vi.stubGlobal("fetch", fetch);
    const events: ChatEvent[] = [];
    await streamChat(
      { messages: [{ role: "user", content: "测试问题" }] },
      new AbortController().signal,
      (event) => events.push(event)
    );
    expect(events.map((event) => event.payload.type)).toEqual([
      "turn.started",
      "message.delta",
      "message.delta",
      "turn.completed",
    ]);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]).toMatchObject([
      "/api/chat/turn",
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": "test-csrf" },
      },
    ]);
  });

  it("已有正文却提前断线时失败，不自动重新请求或伪造完成", async () => {
    setCsrfToken("test-csrf");
    const fetch = vi
      .fn()
      .mockResolvedValue(
        response(
          frame(1, { type: "turn.started" }) +
            frame(2, { type: "message.delta", text: "部分回复" })
        )
      );
    vi.stubGlobal("fetch", fetch);
    const events: ChatEvent[] = [];
    await expect(
      streamChat({ messages: [] }, new AbortController().signal, (event) =>
        events.push(event)
      )
    ).rejects.toMatchObject({ code: "chat_protocol_error" });
    expect(events).toHaveLength(2);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    frame(2, { type: "turn.started" }),
    frame(1, { type: "turn.started" }) +
      frame(2, { type: "message.delta", text: "错误轮次" }, "other-turn"),
    frame(1, { type: "turn.started" }) +
      frame(1, { type: "turn.completed", text: "重复序号" }),
  ])("拒绝乱序、串轮与重复帧", async (text) => {
    setCsrfToken("test-csrf");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(text)));
    await expect(
      streamChat({ messages: [] }, new AbortController().signal, vi.fn())
    ).rejects.toMatchObject({ code: "chat_protocol_error" });
  });

  it("取消发生在取令牌之后、实际发起请求之前时，不发出模型请求", async () => {
    setCsrfToken("test-csrf");
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    controller.abort();
    await expect(
      streamChat({ messages: [] }, controller.signal, vi.fn())
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("SSE 发现会话失效时通知全局登录状态", async () => {
    setCsrfToken("test-csrf");
    const listener = vi.fn(),
      remove = onSessionExpired(listener);
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          response(
            frame(1, {
              type: "turn.failed",
              code: "not_authenticated",
              message: "登录已失效",
            })
          )
        )
    );
    try {
      await streamChat({ messages: [] }, new AbortController().signal, vi.fn());
      expect(listener).toHaveBeenCalledTimes(1);
    } finally {
      remove();
    }
  });

  it("流式 POST 只在业务前 CSRF 拒绝时重新取令牌并重试一次", async () => {
    setCsrfToken("old-test-token");
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            code: "csrf_failed",
            message: "令牌失效",
            request_id: "test",
          }),
          { status: 403, headers: { "Content-Type": "application/json" } }
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ csrf_token: "new-test-token", authenticated: true }),
          { headers: { "Content-Type": "application/json" } }
        )
      )
      .mockResolvedValueOnce(
        response(
          frame(1, { type: "turn.started" }) +
            frame(2, { type: "turn.completed", text: "成功" })
        )
      );
    vi.stubGlobal("fetch", fetch);
    await streamChat({ messages: [] }, new AbortController().signal, vi.fn());
    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      "/api/chat/turn",
      "/api/auth/csrf",
      "/api/chat/turn",
    ]);
  });

  it.each(["not-json", "{}", '{"sequence":1,"payload":{"type":"unknown"}}'])(
    "损坏内容只给安全错误",
    (value) => {
      expect(() => parseChatEvent(value)).toThrow("回复连接异常");
    }
  );
});

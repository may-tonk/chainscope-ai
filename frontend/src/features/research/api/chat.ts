/** 主 LLM SSE 适配：支持 UTF-8 分片、CRLF、多行 data、心跳及终态校验。 */
import {
  ApiError,
  apiStreamRequest,
  notifySessionExpired,
} from "../../../shared/api/client";
import type { ChatEvent, ChatInput, ChatPayload } from "../contracts/session";

/** 不把损坏的响应或供应商原文回显给用户。 */
function protocolError(): ApiError {
  return new ApiError(
    0,
    "chat_protocol_error",
    "回复连接异常，本轮未能完成，请重试。"
  );
}

/** 校验页面实际使用的字段；审计字段可向后兼容，核心事件不得猜测。 */
export function parseChatEvent(text: string): ChatEvent {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw protocolError();
  }
  if (!value || typeof value !== "object") throw protocolError();
  const event = value as Record<string, unknown>;
  if (
    typeof event.session_id !== "string" ||
    !event.session_id ||
    typeof event.turn_id !== "string" ||
    !event.turn_id ||
    !Number.isSafeInteger(event.sequence) ||
    (event.sequence as number) < 1 ||
    !event.payload ||
    typeof event.payload !== "object"
  )
    throw protocolError();
  const p = event.payload as Record<string, unknown>;
  switch (p.type) {
    case "turn.started":
      break;
    case "message.delta":
    case "turn.completed":
      if (typeof p.text !== "string" || !p.text) throw protocolError();
      break;
    case "turn.failed":
      if (
        typeof p.code !== "string" ||
        typeof p.message !== "string" ||
        !p.message
      )
        throw protocolError();
      break;
    case "turn.interrupted":
      if (p.reason !== "superseded" && p.reason !== "user_stop")
        throw protocolError();
      break;
    case "decision.ready": {
      if (!p.decision || typeof p.decision !== "object") throw protocolError();
      const action = (p.decision as Record<string, unknown>).action;
      if (
        !action ||
        typeof action !== "object" ||
        !["reply", "clarify", "delegate"].includes(
          String((action as Record<string, unknown>).kind)
        ) ||
        !["not_requested", "not_connected"].includes(String(p.dispatch_status))
      )
        throw protocolError();
      break;
    }
    default:
      throw protocolError();
  }
  return {
    session_id: event.session_id,
    turn_id: event.turn_id,
    sequence: event.sequence as number,
    ...(Number.isSafeInteger(event.request_revision) && (event.request_revision as number) > 0 ? { request_revision: event.request_revision as number } : {}),
    payload: p as ChatPayload,
  };
}

/** 逐帧交付正文，网络提前结束明确失败；取消保持 AbortError，禁止流后自动重试。 */
export async function streamChat(
  input: ChatInput,
  signal: AbortSignal,
  receive: (event: ChatEvent) => void
): Promise<void> {
  const response = await apiStreamRequest("/api/chat/turn", input, signal);
  return consumeChatResponse(response, signal, receive);
}

/** 共用 SSE 解析与序号校验；持久聊天只更换请求合同及地址，不复制协议解析。 */
export async function consumeChatResponse(
  response: Response,
  signal: AbortSignal,
  receive: (event: ChatEvent) => void
): Promise<void> {
  if (
    !response.headers.get("Content-Type")?.includes("text/event-stream") ||
    !response.body
  ) {
    await response.body?.cancel();
    throw protocolError();
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "",
    sequence = 0,
    turn = "",
    session = "",
    terminal = false;
  /** 取消客户端读取，唤醒 pending read；后端会结束本轮上游等待。 */
  const abort = () => {
    void reader.cancel().catch(() => undefined);
  };
  signal.addEventListener("abort", abort, { once: true });
  /** 心跳只作注释，数据帧须保持单轮标识与严格递增序号。 */
  function frame(raw: string) {
    if (raw.length > 131072) throw protocolError();
    const data = raw
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) return;
    const event = parseChatEvent(data);
    if (
      terminal ||
      event.sequence !== sequence + 1 ||
      (turn && (turn !== event.turn_id || session !== event.session_id)) ||
      (!turn && !["turn.started", "turn.failed"].includes(event.payload.type))
    )
      throw protocolError();
    sequence = event.sequence;
    turn = event.turn_id;
    session = event.session_id;
    terminal = ["turn.completed", "turn.failed", "turn.interrupted"].includes(
      event.payload.type
    );
    if (
      event.payload.type === "turn.failed" &&
      event.payload.code === "not_authenticated"
    )
      notifySessionExpired();
    receive(event);
  }
  try {
    while (true) {
      signal.throwIfAborted();
      const { value, done } = await reader.read();
      signal.throwIfAborted();
      buffer += done
        ? decoder.decode()
        : decoder.decode(value, { stream: true });
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        frame(buffer.slice(0, boundary.index));
        buffer = buffer.slice(boundary.index + boundary[0].length);
      }
      if (buffer.length > 131072) throw protocolError();
      if (done) break;
    }
    if (buffer.trim()) frame(buffer);
    if (!terminal) throw protocolError();
  } finally {
    signal.removeEventListener("abort", abort);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

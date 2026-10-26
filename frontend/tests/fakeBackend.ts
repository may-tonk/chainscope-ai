/**
 * 测试用的“假后端”：替换全局 fetch，按顺序返回预先准备好的响应，并记录前端发出的每个请求。
 *
 * 这样测试不需要启动真实后端，就能检查前端“发了什么请求、带了什么请求头、
 * 收到各种响应后怎么处理”。真实后端的联调另外在浏览器里做（方案 3.4 实施顺序）。
 */
import { vi } from "vitest";

/** 一条预先准备的响应。 */
export interface FakeResponse {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
  /** 为 true 时 body 按纯文本返回（模拟后端没启动时开发服务器返回的错误页）。 */
  text?: boolean;
}

/** 前端发出的一个请求的记录。 */
export interface RecordedRequest {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

/** 生成 JSON 响应。 */
export function json(status: number, body: unknown, headers: Record<string, string> = {}): FakeResponse {
  return { status, body, headers };
}

/** 生成后端统一格式的错误响应。 */
export function apiError(
  status: number,
  code: string,
  message: string,
  headers: Record<string, string> = {},
): FakeResponse {
  return json(status, { code, message, request_id: `req-${code}` }, headers);
}

/**
 * 安装假后端。responses 按请求顺序依次返回；请求次数超过准备的响应数量时测试失败，
 * 这样“前端多发了意料之外的请求”也能被发现。
 */
export function installFakeBackend(responses: FakeResponse[]) {
  const requests: RecordedRequest[] = [];
  const queue = [...responses];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = Object.fromEntries(new Headers(init?.headers).entries());
    requests.push({
      method: init?.method ?? "GET",
      path: String(input),
      headers,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    });
    const next = queue.shift();
    if (next === undefined) {
      throw new Error(`假后端没有准备第 ${requests.length} 个请求的响应：${String(input)}`);
    }
    const responseHeaders = new Headers(next.headers);
    if (next.text) {
      responseHeaders.set("Content-Type", "text/plain");
      return new Response(String(next.body ?? ""), { status: next.status, headers: responseHeaders });
    }
    responseHeaders.set("Content-Type", "application/json");
    return new Response(JSON.stringify(next.body ?? {}), {
      status: next.status,
      headers: responseHeaders,
    });
  });

  vi.stubGlobal("fetch", fetchMock);
  return { requests, fetchMock };
}

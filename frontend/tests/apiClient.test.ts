/**
 * 请求封装（src/shared/api/client.ts）的测试：
 * CSRF 请求头、统一错误格式、令牌失效后只重试一次、各种连接失败的处理。
 */
import { describe, expect, it, vi } from "vitest";

import { ApiError, apiRequest, loadCsrf, setCsrfToken } from "../src/shared/api/client";
import { apiError, installFakeBackend, json } from "./fakeBackend";

const CSRF = { csrf_token: "token-1", authenticated: false };

describe("CSRF 令牌", () => {
  it("修改类请求前没有令牌时，先取令牌，再把它放进 X-CSRF-Token 请求头", async () => {
    const { requests } = installFakeBackend([json(200, CSRF), json(200, { message: "ok" })]);

    await apiRequest("POST", "/api/auth/logout");

    expect(requests.map((r) => `${r.method} ${r.path}`)).toEqual([
      "GET /api/auth/csrf",
      "POST /api/auth/logout",
    ]);
    expect(requests[1]?.headers["x-csrf-token"]).toBe("token-1");
  });

  it("已有令牌时直接使用，不重复去取", async () => {
    setCsrfToken("token-memory");
    const { requests } = installFakeBackend([json(200, { message: "ok" })]);

    await apiRequest("POST", "/api/auth/logout");

    expect(requests).toHaveLength(1);
    expect(requests[0]?.headers["x-csrf-token"]).toBe("token-memory");
  });

  it("GET 请求不带 CSRF 请求头", async () => {
    const { requests } = installFakeBackend([json(200, { email: "a@example.com" })]);

    await apiRequest("GET", "/api/auth/me");

    expect(requests[0]?.headers["x-csrf-token"]).toBeUndefined();
  });

  it("多个请求同时需要令牌时，只向后端要一次", async () => {
    const { requests } = installFakeBackend([json(200, CSRF)]);

    await Promise.all([loadCsrf(), loadCsrf(), loadCsrf()]);

    expect(requests).toHaveLength(1);
  });

  it("令牌失效（csrf_failed）时重新取令牌并重试一次，重试成功就返回结果", async () => {
    setCsrfToken("old-token");
    const { requests } = installFakeBackend([
      apiError(403, "csrf_failed", "页面已过期，请刷新后重试"),
      json(200, { csrf_token: "new-token", authenticated: false }),
      json(200, { message: "已退出" }),
    ]);

    const result = await apiRequest<{ message: string }>("POST", "/api/auth/logout");

    expect(result.message).toBe("已退出");
    expect(requests.map((r) => r.headers["x-csrf-token"])).toEqual([
      "old-token",
      undefined,
      "new-token",
    ]);
  });

  it("重试后仍然 csrf_failed 时不再重试，抛出错误（不会无限循环）", async () => {
    setCsrfToken("old-token");
    const { requests } = installFakeBackend([
      apiError(403, "csrf_failed", "页面已过期，请刷新后重试"),
      json(200, CSRF),
      apiError(403, "csrf_failed", "页面已过期，请刷新后重试"),
    ]);

    await expect(apiRequest("POST", "/api/auth/logout")).rejects.toMatchObject({
      code: "csrf_failed",
    });
    expect(requests).toHaveLength(3);
  });
});

describe("错误格式", () => {
  it("把后端的统一错误转换成 ApiError，带上请求编号和 Retry-After 秒数", async () => {
    setCsrfToken("t");
    installFakeBackend([
      apiError(429, "rate_limited", "尝试次数过多，请稍后再试", { "Retry-After": "42" }),
    ]);

    const error = await apiRequest("POST", "/api/auth/login", { email: "a", password: "b" }).catch(
      (e: unknown) => e,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 429,
      code: "rate_limited",
      message: "尝试次数过多，请稍后再试",
      requestId: "req-rate_limited",
      retryAfter: 42,
    });
  });

  it("后端没启动、开发服务器返回非 JSON 错误页时，显示通用的“服务暂时不可用”", async () => {
    installFakeBackend([{ status: 502, body: "Bad Gateway", text: true }]);

    await expect(apiRequest("GET", "/api/auth/me")).rejects.toMatchObject({
      status: 502,
      code: "service_unavailable",
      message: "服务暂时不可用，请稍后再试",
    });
  });

  it("完全连不上（fetch 抛错）时，返回 network_error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    await expect(apiRequest("GET", "/api/auth/me")).rejects.toMatchObject({
      status: 0,
      code: "network_error",
    });
  });

  it("拒绝不是 /api/ 开头的地址，防止把带 Cookie 的请求发到站外", async () => {
    await expect(apiRequest("GET", "https://evil.example/api/x")).rejects.toThrow(/站内路径/);
  });
});

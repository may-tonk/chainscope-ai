/**
 * 登录后跳转目标的安全检查（src/features/auth/session/redirect.ts）的测试：
 * 只接受站内路径，防止“开放重定向”把刚登录的用户带去钓鱼网站。
 */
import { describe, expect, it } from "vitest";

import { loginPathFor, safeNextPath } from "../src/features/auth/session/redirect";

describe("safeNextPath", () => {
  it.each([
    ["/settings", "/settings"],
    ["/research/bem?tab=flows#top", "/research/bem?tab=flows#top"],
  ])("接受站内路径 %s", (input, expected) => {
    expect(safeNextPath(input)).toBe(expected);
  });

  it.each([
    [null, "没有参数"],
    ["", "空字符串"],
    ["https://evil.example", "完整的外部网址"],
    ["javascript:alert(1)", "javascript 伪协议"],
    ["//evil.example", "双斜杠：浏览器当成另一个网站"],
    ["/\\evil.example", "斜杠加反斜杠：部分浏览器等同于双斜杠"],
    ["/\tevil", "含控制字符（制表符）"],
    ["/login", "登录页本身"],
    ["/login?next=%2F", "登录页带参数"],
  ])("拒绝 %s（%s），回到首页", (input, reason) => {
    // 第二个参数是拒绝原因，失败时显示在报错信息里，方便看出是哪一类没拦住。
    expect(safeNextPath(input), reason).toBe("/");
  });
});

describe("loginPathFor", () => {
  it("把当前地址编码进 next 参数", () => {
    expect(loginPathFor("/settings?tab=security")).toBe("/login?next=%2Fsettings%3Ftab%3Dsecurity");
  });

  it("本来就去首页时不带参数", () => {
    expect(loginPathFor("/")).toBe("/login");
  });
});

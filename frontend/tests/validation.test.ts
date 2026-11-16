/**
 * 前端校验规则（src/shared/validation/）的测试。
 * 邮箱和密码长度的用例与后端 python/tests/webapp 中的规则对应，保证前后端判断一致。
 */
import { describe, expect, it } from "vitest";

import { cleanCode, validateCode } from "../src/shared/validation/code";
import { validateEmail } from "../src/shared/validation/email";
import { passwordLength, validateNewPassword } from "../src/shared/validation/password";

describe("validateEmail（与后端 core/emails.py 一致）", () => {
  it.each(["user@example.com", "  first.last+tag@sub.example.cn  ", "a_b-c@x-y.io"])(
    "接受 %s",
    (email) => {
      expect(validateEmail(email)).toBeNull();
    },
  );

  it("空输入提示“请输入邮箱”", () => {
    expect(validateEmail("   ")).toBe("请输入邮箱");
  });

  it.each([
    ["没有 @", "user.example.com"],
    ["两个 @", "a@b@example.com"],
    ["@ 前为空", "@example.com"],
    ["点在开头", ".user@example.com"],
    ["连续两个点", "a..b@example.com"],
    ["域名只有一段", "user@localhost"],
    ["顶级域名有数字", "user@example.c0m"],
    ["顶级域名只有 1 个字母", "user@example.c"],
    ["域名段以连字符开头", "user@-example.com"],
    ["中文邮箱", "用户@example.com"],
    ["@ 前超过 64 个字符", `${"a".repeat(65)}@example.com`],
    ["总长超过 254", `user@${"a".repeat(250)}.com`],
  ])("拒绝：%s", (_reason, email) => {
    expect(validateEmail(email)).toBe("邮箱格式不正确");
  });
});

describe("密码长度（与后端 security/passwords.py 一致：NFC 后按码点计数）", () => {
  it("一个表情符号算 1 个字符（JavaScript 的 length 会算成 2）", () => {
    expect("😀".length).toBe(2);
    expect(passwordLength("😀")).toBe(1);
  });

  it("同一个带声调的字母，两种写法按规范化后的长度计算", () => {
    // "e" + 组合声调符号，NFC 规范化后变成一个字符 "é"。
    expect(passwordLength("é")).toBe(1);
  });

  it("7 个字符太短，8 个字符可以", () => {
    expect(validateNewPassword("abcdefg")).toBe("密码至少 8 个字符");
    expect(validateNewPassword("abcdefgh")).toBeNull();
  });

  it("中文和空格也计入长度", () => {
    expect(validateNewPassword("链上 研究 很有趣")).toBeNull(); // 9 个字符
  });

  it("128 个字符可以，129 个太长", () => {
    expect(validateNewPassword("a".repeat(128))).toBeNull();
    expect(validateNewPassword("a".repeat(129))).toBe("密码最多 128 个字符");
  });

  it("没填时提示“请设置密码”", () => {
    expect(validateNewPassword("")).toBe("请设置密码");
  });
});

describe("验证码输入整理", () => {
  it.each([
    ["123 456", "123456"],
    ["12-34-56", "123456"],
    ["１２３４５６", "123456"], // 全角数字
    ["1234567", "123456"], // 超出 6 位的部分截掉
    ["abc", ""],
  ])("%s → %s", (raw, cleaned) => {
    expect(cleanCode(raw)).toBe(cleaned);
  });

  it("不足 6 位时提示", () => {
    expect(validateCode("")).toBe("请输入验证码");
    expect(validateCode("12345")).toBe("验证码是 6 位数字");
    expect(validateCode("123456")).toBeNull();
  });
});

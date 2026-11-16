/**
 * 每个测试文件运行前的准备工作（vite.config.ts 的 test.setupFiles 指向这里）。
 *
 * - 加载 jest-dom：提供 toBeInTheDocument()、toHaveTextContent() 等检查页面内容的断言；
 * - 每个测试结束后：清空渲染出来的页面、恢复被替换的全局函数（例如模拟的 fetch），
 *   保证测试之间互不影响。
 */
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { setCsrfToken } from "../src/shared/api/client";

// jsdom（测试用的模拟浏览器）还没有实现 <dialog> 的 showModal() / close()，真实浏览器（包括 Edge）都支持。
// 这里只在测试环境补一个最简单的版本：打开就设置 open 属性，关闭就去掉。网站代码不受影响。
if (typeof HTMLDialogElement !== "undefined" && !HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  // 请求模块会把 CSRF 令牌记在内存里，测试之间要清掉，否则上一个测试的令牌会影响下一个。
  setCsrfToken(null);
});

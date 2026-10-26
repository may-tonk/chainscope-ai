/**
 * 前端入口：浏览器加载 index.html 后执行这个文件，把 React 应用渲染进 <div id="root">。
 *
 * 样式加载顺序：先设计变量（tokens.css），再全局样式（global.css），
 * 后面各组件的样式都会引用前面定义的变量。
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./shared/styles/tokens.css";
import "./shared/styles/global.css";
import { App } from "./app/App";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("index.html 里缺少 id=\"root\" 的元素，React 无处渲染");
}

// StrictMode：开发模式下额外做一些检查（例如故意把 useEffect 执行两次，暴露清理不干净的问题），
// 正式打包后不起作用，不影响性能。
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

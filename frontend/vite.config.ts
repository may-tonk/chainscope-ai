/**
 * Vite 配置：开发服务器、`/api` 转发、生产打包，以及 Vitest 测试环境。
 *
 * 开发时浏览器只访问 http://localhost:5173 这一个地址：
 * - 页面由 Vite 提供；
 * - 以 /api 开头的请求由 Vite 转发给本机后端 127.0.0.1:8000。
 * 这样在浏览器看来前端和后端是“同一个网站”（同源），会话 Cookie 能正常带上，
 * 也不需要配置跨域（CORS）。上线后由服务器或反向代理实现同样的效果，见方案 3.2。
 */
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],

  server: {
    // 固定用 localhost:5173。后端只接受来源为 http://localhost:5173 的修改请求
    // （WEBAPP_ALLOWED_ORIGINS 的开发默认值），用 127.0.0.1:5173 打开会被拒绝（403）。
    host: "localhost",
    port: 5173,
    // 端口被占用时直接报错，而不是悄悄换成 5174：换了端口，后端的来源检查就会失败。
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        // false：转发时保留浏览器原来的 Host（localhost:5173）。
        // 后端的 TrustedHostMiddleware 只认 localhost / 127.0.0.1，
        // Origin 请求头也原样转发，后端的来源检查才能看到真实的 http://localhost:5173。
        changeOrigin: false,
      },
    },
  },

  test: {
    // 测试在 jsdom（用 JavaScript 模拟的浏览器环境）里运行，不需要打开真实浏览器。
    environment: "jsdom",
    // 每个测试文件运行前先执行的准备工作（加载断言扩展、清理页面）。
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});

/**
 * ESLint 代码规范检查配置。运行：npm run lint
 *
 * 检查三类问题：
 * - JavaScript 通用错误（@eslint/js 推荐规则）；
 * - TypeScript 相关错误（typescript-eslint 推荐规则）；
 * - React Hooks 的使用规则（例如 useEffect 漏写依赖），这类错误很隐蔽，靠工具发现。
 */
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

export default defineConfig(
  // 不检查依赖和打包结果。
  { ignores: ["node_modules", "dist"] },
  js.configs.recommended,
  tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
);

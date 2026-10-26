/**
 * 读取当前页面在路由表里声明的附加信息（RouteHandle：标题、板块导航）。
 *
 * 外壳（AppLayout）用它显示顶栏标题和板块导航，根布局（RootLayout）用它设置浏览器标签页标题。
 * 放在一个 Hook 里，两处共用同一套规则，不各写一遍。
 */
import { useMatches } from "react-router";

import type { RouteHandle } from "./navigation";

/**
 * 从当前匹配到的各级路由里，找出最深一级声明的 handle。
 * 例如以后的“/research/bem/flows”同时匹配“研究”和“资金流向”两级路由，取更具体的那一级。
 */
export function useRouteHandle(): RouteHandle {
  const matches = useMatches();
  for (let i = matches.length - 1; i >= 0; i -= 1) {
    const handle = matches[i]?.handle as RouteHandle | undefined;
    if (handle) {
      return handle;
    }
  }
  return {};
}

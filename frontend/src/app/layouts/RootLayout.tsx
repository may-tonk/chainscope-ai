/**
 * 根布局：路由表最外层，所有页面都经过这里。本身不显示任何内容，只做全站共用的小事：
 *
 * - 浏览器标签页标题：按当前路由 handle 里的 title 设置为“研究空间 · ChainScope”这样的格式。
 *   新增页面只要在路由表里写上 title，标签页标题自动正确，不需要每个页面自己设置。
 *
 * 以后如果有其他“每次换页都要做的事”（例如换页后把焦点移到页面标题，方便读屏软件），也放在这里。
 */
import { useEffect } from "react";
import { Outlet } from "react-router";

import { useRouteHandle } from "../useRouteHandle";

/** 网站名，标签页标题的后半部分。 */
const SITE_NAME = "ChainScope";

/** 根布局组件：同步标签页标题，然后原样显示子路由（<Outlet />）。 */
export function RootLayout() {
  const { title } = useRouteHandle();

  useEffect(() => {
    document.title = title ? `${title} · ${SITE_NAME}` : SITE_NAME;
  }, [title]);

  return <Outlet />;
}

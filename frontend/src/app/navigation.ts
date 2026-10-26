/**
 * 导航配置：工作台左侧栏显示哪些入口，全部由这里的列表决定。
 *
 * 这是前端最主要的“扩展点”（方案 3.4）：以后加一个新功能（例如“我的报告”），
 * 只需要把对应项的 available 改成 true，并在 app/router.tsx 里加上它的路由；
 * 左侧栏、手机菜单等外壳代码都不用改。
 *
 * 导航分两级：
 * 1. 全局导航（primaryNavigation）：任何页面都显示，例如研究空间、我的报告；
 * 2. 板块导航（RouteHandle.sectionNavigation）：只在进入某个板块后显示。
 *    例如以后进入“BEM 研究”时，显示研究概览、资金流向、发行与持仓等子页面。
 *    板块导航由该板块的路由在 handle 里声明，外壳自动读取并显示（见 AppLayout）。
 */
import {
  ChartColumn,
  FileText,
  LayoutDashboard,
  Settings,
  UserRound,
  type LucideIcon,
} from "lucide-react";

/** 导航里的一个入口。 */
export interface NavItem {
  /** 唯一标识，用作列表的 key。 */
  id: string;
  /** 显示的文字。 */
  label: string;
  /**
   * 一句话说明这个功能做什么。未上线时，工作台首页的“即将上线”卡片显示这句话；
   * 上线后也可用作鼠标悬停提示。说明只写在这里，首页不另写一份。
   */
  description: string;
  /** 左侧的线条图标（lucide-react）。 */
  icon: LucideIcon;
  /** 点击后去的站内地址。 */
  path: string;
  /**
   * 功能是否已经上线。false 时显示为灰色、带“即将上线”标签、不能点击，
   * 让用户看到网站的全貌，但不假装功能可用（2026-10-25 用户确认）。
   */
  available: boolean;
}

/** 一组导航入口，可以带一个小标题。 */
export interface NavGroup {
  id: string;
  title?: string;
  items: NavItem[];
}

/**
 * 路由的附加信息（React Router 的 handle 字段）。页面在路由表里声明，外壳读取后显示。
 * - title：顶栏显示的页面名称；
 * - sectionNavigation：进入这个板块后，左侧栏额外显示的板块导航。
 */
export interface RouteHandle {
  title?: string;
  sectionNavigation?: NavGroup;
}

/** 全局导航：对应参考图左侧栏的四个入口。第一版只有“研究空间”可用（工作台首页）。 */
export const primaryNavigation: NavGroup = {
  id: "primary",
  items: [
    {
      id: "workspace",
      label: "研究空间",
      description: "你的研究从这里开始：查看最近的研究和账户信息。",
      icon: LayoutDashboard,
      path: "/",
      available: true,
    },
    {
      id: "reports",
      label: "我的报告",
      description: "每次研究整理成的报告，可以反复查看、持续补充新版本。",
      icon: FileText,
      path: "/reports",
      available: false,
    },
    {
      id: "watchlist",
      label: "地址与关注",
      description: "给关注的链上地址做标记，跟踪它们的资金变化。",
      icon: UserRound,
      path: "/watchlist",
      available: false,
    },
    {
      id: "methods",
      label: "分析方法",
      description: "把常用的分析步骤保存成方法，在新的研究中直接复用。",
      icon: ChartColumn,
      path: "/methods",
      available: false,
    },
  ],
};

/** 账户相关的入口，显示在左下角用户区（实施第 5 步完成，2026-10-26 上线）。 */
export const accountNavigation: NavItem = {
  id: "settings",
  label: "设置",
  description: "修改密码、退出所有设备、注销账户。",
  icon: Settings,
  path: "/settings",
  available: true,
};

/**
 * 还没上线的全局导航功能。工作台首页用它生成“即将上线”卡片。
 * 某个功能上线（available 改成 true）后，它会自动从首页卡片中消失、在侧栏变成可点击。
 */
export function upcomingFeatures(): NavItem[] {
  return primaryNavigation.items.filter((item) => !item.available);
}

/** 左侧栏顶部的主按钮。研究功能上线前保持不可用。 */
export const primaryAction = {
  label: "新建研究",
  available: false,
} as const;

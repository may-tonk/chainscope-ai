/**
 * 把一组导航配置（NavGroup）画成左侧栏里的一列入口。
 *
 * - 已上线的入口：用 NavLink 渲染，当前所在页面自动高亮（浅绿底、深绿字）；
 * - 未上线的入口：灰色文字加“即将上线”标签，不是链接，点击没有反应；
 *   用 aria-disabled 告诉读屏软件它不可用。
 */
import { NavLink } from "react-router";

import type { NavGroup } from "../navigation";
import styles from "./NavList.module.css";

interface NavListProps {
  group: NavGroup;
  /** 点击任一入口后调用（手机上用来收起侧边栏）。 */
  onNavigate?: () => void;
}

/** 导航列表组件：逐项渲染 group 里的入口，已上线的是链接，未上线的是灰色文字。 */
export function NavList({ group, onNavigate }: NavListProps) {
  return (
    <div className={styles.group}>
      {group.title && <p className={styles.title}>{group.title}</p>}
      <ul className={styles.list}>
        {group.items.map((item) => {
          const Icon = item.icon;
          return (
            <li key={item.id}>
              {item.available ? (
                <NavLink
                  to={item.path}
                  // end：只有地址完全相同才高亮。否则 "/" 会匹配所有页面，首页入口永远是高亮的。
                  end
                  className={({ isActive }) =>
                    isActive ? `${styles.item} ${styles.active}` : styles.item
                  }
                  onClick={onNavigate}
                >
                  <Icon size={18} aria-hidden="true" />
                  <span>{item.label}</span>
                </NavLink>
              ) : (
                <span className={`${styles.item} ${styles.disabled}`} aria-disabled="true">
                  <Icon size={18} aria-hidden="true" />
                  <span>{item.label}</span>
                  <span className={styles.badge}>即将上线</span>
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

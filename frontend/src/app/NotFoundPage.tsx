/**
 * 找不到页面（任何没有定义的地址）。所有人都能看到，不需要登录。
 *
 * “返回首页”指向 /：已登录会进工作台，未登录会被 RequireAuth 送去登录页。
 */
import { Link } from "react-router";

import styles from "./NotFoundPage.module.css";

/** 404 页面组件：说明地址不存在，并给出返回首页的链接。 */
export function NotFoundPage() {
  return (
    <main className={styles.screen}>
      <div className={styles.card}>
        <p className={styles.code}>404</p>
        <h1 className={styles.title}>页面不存在</h1>
        <p className={styles.text}>这个地址没有对应的页面，可能输入有误，或者功能还没有上线。</p>
        <Link to="/" className={styles.link}>
          返回首页
        </Link>
      </div>
    </main>
  );
}

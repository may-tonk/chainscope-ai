/**
 * 整页加载画面：打开网站、正在向后端确认登录状态时显示。
 *
 * 只显示 Logo 和一个转圈图标，不显示任何页面内容，
 * 这样确认完成前不会出现“先闪一下登录页、再跳到工作台”的情况。
 */
import { LoaderCircle } from "lucide-react";

import styles from "./LoadingScreen.module.css";

/** 整页加载画面组件。label 是给读屏软件读的文字，屏幕上不显示。 */
export function LoadingScreen({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className={styles.screen} role="status" aria-live="polite">
      <span className={styles.brand}>ChainScope</span>
      <LoaderCircle className={styles.spinner} size={22} aria-hidden="true" />
      <span className="visually-hidden">{label}</span>
    </div>
  );
}

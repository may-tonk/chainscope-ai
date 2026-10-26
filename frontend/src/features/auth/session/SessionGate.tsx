/**
 * “登录状态确认完成之前，先别显示页面”的关口，包在路由外面（见 app/App.tsx）。
 *
 * - loading：显示整页加载画面；
 * - error：后端连不上或数据库不可用，显示原因和“重试”按钮；
 *   这时不能假装“未登录”把用户送去登录页，否则用户会以为自己被退出了，反复登录也没用；
 * - 其他（anonymous / authenticated）：确认完成，正常显示页面。
 */
import type { ReactNode } from "react";

import { Alert } from "../../../shared/ui/Alert";
import { Button } from "../../../shared/ui/Button";
import { LoadingScreen } from "../../../shared/ui/LoadingScreen";
import { useAuth } from "./AuthContext";
import styles from "./SessionGate.module.css";

/** 按登录状态决定显示加载画面、错误页，还是正常显示 children（路由和页面）。 */
export function SessionGate({ children }: { children: ReactNode }) {
  const { state, reload } = useAuth();

  if (state.status === "loading") {
    return <LoadingScreen label="正在确认登录状态…" />;
  }

  if (state.status === "error") {
    return (
      <main className={styles.screen}>
        <div className={styles.card}>
          <h1 className={styles.title}>暂时无法连接 ChainScope</h1>
          <Alert variant="error" requestId={state.error.requestId}>
            {state.error.message}
          </Alert>
          <Button block onClick={() => void reload()}>
            重试
          </Button>
        </div>
      </main>
    );
  }

  return children;
}

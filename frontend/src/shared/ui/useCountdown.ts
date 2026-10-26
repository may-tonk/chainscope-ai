/**
 * 倒计时：被限速（429）时，按后端给的 Retry-After 秒数倒数，倒数期间禁用提交按钮。
 *
 * 用法：
 *   const countdown = useCountdown();
 *   countdown.start(error.retryAfter);   // 开始倒数
 *   countdown.remaining                  // 还剩几秒；0 表示可以再试
 *   countdown.text                       // 剩余时间的中文写法，例如“42 秒”“25 分 25 秒”
 *
 * 也可以一开始就倒数：useCountdown(60)。例如验证码刚发出，“重新发送”按钮要先等 60 秒。
 *
 * 这只是为了提示用户，不是安全措施：真正的限速在后端，前端倒计时被绕过也没用。
 */
import { useCallback, useEffect, useState } from "react";

/**
 * 倒计时 Hook：返回剩余秒数 remaining、剩余时间的中文写法 text，以及开始倒数的 start(seconds)。
 * initialSeconds：组件一出现就开始倒数的秒数；不传则不倒数。
 */
export function useCountdown(initialSeconds?: number) {
  // deadline：倒数结束的时间点（毫秒时间戳）；null 表示没有在倒数。
  // 用函数作为初始值，只在组件第一次渲染时计算一次。
  const [deadline, setDeadline] = useState<number | null>(() =>
    initialSeconds && initialSeconds > 0 ? Date.now() + initialSeconds * 1000 : null,
  );
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (deadline === null) {
      return;
    }
    // 每秒更新一次“现在”，界面上的剩余秒数随之减少；到点后停止计时器。
    const timer = window.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= deadline) {
        window.clearInterval(timer);
        setDeadline(null);
      }
    }, 1000);
    // 组件卸载或重新开始倒数时，清除旧的计时器，避免多个计时器同时运行。
    return () => window.clearInterval(timer);
  }, [deadline]);

  /** 开始倒数 seconds 秒；seconds 为空或不大于 0 时不倒数。 */
  const start = useCallback((seconds: number | null) => {
    if (seconds === null || seconds <= 0) {
      return;
    }
    const current = Date.now();
    setNow(current);
    setDeadline(current + seconds * 1000);
  }, []);

  const remaining = deadline === null ? 0 : Math.max(0, Math.ceil((deadline - now) / 1000));
  // text：给用户看的剩余时间，例如“42 秒”“25 分 25 秒”。各页面统一用它拼提示，不各自换算。
  return { remaining, text: formatDuration(remaining), start };
}

/**
 * 把秒数换成用户容易理解的中文时长：
 * - 不到 1 分钟：“42 秒”；
 * - 1 分钟以上：“25 分 25 秒”，整分钟时“2 分钟”。
 * 后端的限速等待可能长达几十分钟（例如同一邮箱每小时只能发 5 封验证码），
 * 直接显示“1525 秒”很难理解（2026-10-26 在 Edge 截图中发现）。
 */
export function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.ceil(totalSeconds));
  if (seconds < 60) {
    return `${seconds} 秒`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest === 0 ? `${minutes} 分钟` : `${minutes} 分 ${rest} 秒`;
}

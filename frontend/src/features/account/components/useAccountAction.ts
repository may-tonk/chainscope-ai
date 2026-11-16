/**
 * 设置页各个操作（修改密码、退出所有设备、注销账户）共用的提交逻辑。
 *
 * 三个操作的处理方式完全一样，只是调用的接口不同，所以集中写在这个 Hook 里：
 * - 提交中的状态（按钮转圈、防止重复提交）；
 * - 错误分类，按类型放到对应位置：
 *   · 当前密码不对（authentication_failed）→ “当前密码”框下方：“当前密码不正确”；
 *   · 新密码不符合规则（password_too_short 等）→ “新密码”框下方；
 *   · 输错太多次被限速（rate_limited）→ 上方提示条 + 按钮倒计时（例如“14 分 30 秒”）；
 *   · 其他错误（网络、服务繁忙）→ 上方提示条，附请求编号。
 * 以后增加新的设置项（例如修改昵称），也可以直接用它。
 *
 * 为什么“当前密码不对”可以直接说“当前密码不正确”，而登录页只能说“账号或密码错误”：
 * 这里用户已经登录，账户是谁已经确定，不存在借提示探测“某个邮箱是否注册”的问题。
 */
import { useState } from "react";

import { asApiError, type ApiError } from "../../../shared/api/client";
import { useCountdown } from "../../../shared/ui/useCountdown";
import { PASSWORD_ERROR_CODES } from "../../../shared/validation/password";

/** 显示在输入框下方的错误；没有值表示该框没问题。 */
export interface AccountFieldErrors {
  currentPassword?: string;
  newPassword?: string;
}

/** 设置页操作的提交逻辑：返回状态和 run(任务)、setFieldErrors(前端检查结果)。 */
export function useAccountAction() {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [fieldErrors, setFieldErrors] = useState<AccountFieldErrors>({});
  const countdown = useCountdown();

  /**
   * 执行一个操作（调用后端接口）。成功返回 true，失败返回 false，并把错误放到对应位置。
   * 成功后调用方通常会结束会话、跳到登录页，所以这里成功时不再更新状态。
   */
  async function run(task: () => Promise<void>): Promise<boolean> {
    setSubmitting(true);
    setError(null);
    setFieldErrors({});
    try {
      await task();
      return true;
    } catch (caught) {
      const apiError = asApiError(caught);
      if (apiError.code === "authentication_failed") {
        setFieldErrors({ currentPassword: "当前密码不正确" });
      } else if (PASSWORD_ERROR_CODES.has(apiError.code)) {
        setFieldErrors({ newPassword: apiError.message });
      } else {
        setError(apiError);
        if (apiError.code === "rate_limited") {
          countdown.start(apiError.retryAfter);
        }
      }
      setSubmitting(false);
      return false;
    }
  }

  return {
    /** 正在提交。 */
    submitting,
    /** 显示在上方提示条里的错误。 */
    error,
    /** 显示在输入框下方的错误。 */
    fieldErrors,
    /** 前端检查（例如“请输入当前密码”）的结果也通过它显示，和后端错误放在同样的位置。 */
    setFieldErrors,
    /** 被限速时的倒计时；remaining > 0 时按钮不可点。 */
    countdown,
    run,
  };
}

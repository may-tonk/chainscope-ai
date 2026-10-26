/**
 * 账户设置相关的接口函数：每个函数对应后端的一个接口（python/webapp/api/routes_auth.py）。
 *
 * 三个操作都要求：已登录、通过 CSRF 检查、输入当前密码（方案第 9 节、13.1 节）。
 * 成功后后端都会结束当前会话并清除 Cookie，调用方随后调用 useAuth().endSession(原因)，
 * 页面切换为未登录并跳到登录页。
 *
 * 共同的失败情况：
 * - 当前密码不对：authentication_failed（401）；
 * - 当前密码连续输错 5 次：rate_limited（429，冷却 15 分钟，附带等待秒数）；
 * - 登录已失效：not_authenticated（401），请求模块会统一处理（跳到登录页提示“登录已过期”）。
 */
import { apiRequest } from "../../shared/api/client";

/** 后端只返回一句提示的接口。 */
interface MessageResponse {
  message: string;
}

/**
 * 修改密码。成功后所有设备（包括当前这个）都需要用新密码重新登录。
 * 另外的失败情况：新密码不符合规则 password_too_short / password_too_long / password_common（400）。
 */
export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  await apiRequest<MessageResponse>("POST", "/api/auth/password/change", {
    current_password: currentPassword,
    new_password: newPassword,
  });
}

/** 退出所有设备：撤销这个账户的全部会话，包括当前这台。 */
export async function signOutEverywhere(currentPassword: string): Promise<void> {
  await apiRequest<MessageResponse>("POST", "/api/auth/logout-all", {
    current_password: currentPassword,
  });
}

/**
 * 注销账户：立即彻底删除，不可恢复（方案 13.1 节，2026-10-23 用户确认）。
 * 同一邮箱以后可以重新注册，但新账户不继承任何旧数据。
 */
export async function deleteAccount(currentPassword: string): Promise<void> {
  await apiRequest<MessageResponse>("POST", "/api/auth/account/delete", {
    current_password: currentPassword,
  });
}

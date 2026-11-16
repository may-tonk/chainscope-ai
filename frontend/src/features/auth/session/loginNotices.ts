/**
 * 登录页上方的提示：所有“跳到登录页时顺带显示的一句话”都集中定义在这里。
 *
 * 为什么集中：
 * - 提示的文字和颜色只有一处来源，以后改措辞只改这里；
 * - 页面之间只传递提示的“编号”（例如 "registered"），不传文字本身。
 *   跳转时附带的数据保存在浏览器历史里，任何人都能伪造；只接受这里定义过的编号，
 *   就不可能借此在登录页上显示任意文字（例如冒充网站发布的钓鱼提示）。
 *
 * 新增一种提示（例如以后的“密码已修改，请重新登录”）：在 LOGIN_NOTICES 里加一项即可。
 */

/** 提示的颜色类型，对应 shared/ui/Alert 的样式。 */
export type NoticeVariant = "success" | "info" | "warning";

export const LOGIN_NOTICES = {
  /** 注册完成后跳到登录页。 */
  registered: { variant: "success", text: "注册成功，请登录" },
  /** 忘记密码流程完成后跳到登录页。后端重置时会让所有设备退出登录，提示里说明这一点。 */
  password_reset: { variant: "success", text: "密码已重置，所有设备已退出，请用新密码登录" },
  /** 会话过期、被撤销，或在其他设备上退出了所有设备。 */
  expired: { variant: "warning", text: "登录已过期，请重新登录" },
  /** 在设置页修改密码后：后端让所有设备退出，需要用新密码重新登录。 */
  password_changed: { variant: "success", text: "密码已修改，请用新密码登录" },
  /** 在设置页点“退出所有设备”后。 */
  signed_out_everywhere: { variant: "info", text: "已退出所有设备，请重新登录" },
  /** 在设置页注销账户后。同一邮箱以后可以重新注册，但不会继承原来的数据。 */
  account_deleted: { variant: "info", text: "账户已注销，相关数据已删除" },
  // 用户自己点“退出登录”后不显示提示，直接回到干净的登录页（2026-10-26 用户决定，以后再考虑）。
  // 如需恢复，在这里加一项即可，其余代码不用改（退出原因 signed_out 一直都有记录）：
  //   signed_out: { variant: "info", text: "你已退出登录" },
} as const satisfies Record<string, { variant: NoticeVariant; text: string }>;

/** 提示的编号，例如 "registered"。 */
export type LoginNoticeKey = keyof typeof LOGIN_NOTICES;

/** 判断一个字符串是不是定义过的提示编号。不是的一律不显示，见文件开头的安全说明。 */
export function isLoginNoticeKey(value: unknown): value is LoginNoticeKey {
  return typeof value === "string" && Object.hasOwn(LOGIN_NOTICES, value);
}

/** 其他页面跳到登录页时可以带上的信息（React Router 的路由状态）。 */
export interface LoginPageState {
  /** 预先填好的邮箱（例如注册成功后）。 */
  email?: string;
  /** 要显示的提示编号。 */
  notice?: LoginNoticeKey;
}

/**
 * 读取并检查路由状态。路由状态来自浏览器历史，可能是任何内容，所以逐项检查：
 * 邮箱必须是字符串，提示编号必须是 LOGIN_NOTICES 里定义过的，其余一律忽略。
 */
export function readLoginPageState(state: unknown): LoginPageState {
  if (typeof state !== "object" || state === null) {
    return {};
  }
  const { email, notice } = state as Record<string, unknown>;
  return {
    email: typeof email === "string" ? email : undefined,
    notice: isLoginNoticeKey(notice) ? notice : undefined,
  };
}

/**
 * 新密码的长度规则，与后端 python/webapp/security/passwords.py 保持一致（方案 5.1 节）。
 *
 * - 长度 8～128 个字符（2026-10-25 用户确认）；
 * - 不要求必须有大写、数字、符号：按 NIST 建议，长度比组合规则更有效；
 * - 空格、中文、表情符号都可以用。
 *
 * “太常见的密码”（如 12345678、password）只在后端检查：常见密码表在服务器上，
 * 不下发到浏览器。后端拒绝时返回 password_common，页面显示在密码框下方。
 *
 * 为什么不直接用 password.length：
 * JavaScript 的 length 按“UTF-16 单位”计数，一个表情符号会算成 2；
 * 同一个带声调的字母也可能有两种写法（一个码点，或字母加声调两个码点）。
 * 后端先做 NFC 规范化、再按“码点”计数，这里用同样的方法，前后端算出的长度才一致。
 */

export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 128;

/**
 * 后端对“新密码不符合规则”使用的错误编号（python/webapp/security/passwords.py 的 PasswordRejected）。
 * 收到这些错误时，提示显示在新密码框下方，而不是页面上方。注册、重置、修改密码共用。
 */
export const PASSWORD_ERROR_CODES: ReadonlySet<string> = new Set([
  "password_too_short",
  "password_too_long",
  "password_common",
]);

/** 按后端的方式计算密码长度：NFC 规范化后的码点个数。 */
export function passwordLength(password: string): number {
  // [...str] 按码点拆分字符串（表情符号算 1 个），而不是按 UTF-16 单位。
  return [...password.normalize("NFC")].length;
}

/**
 * 新密码输入框下方的实时提示：没输入时说明规则；输入后显示已输入多少个字符（按后端的方法计数）。
 * 注册、重置密码、修改密码三处共用，规则改了只改这里。
 */
export function newPasswordHint(password: string): string {
  const length = passwordLength(password);
  return length === 0
    ? `${PASSWORD_MIN_LENGTH}～${PASSWORD_MAX_LENGTH} 个字符，可以包含空格、中文和符号`
    : `已输入 ${length} 个字符（需要 ${PASSWORD_MIN_LENGTH}～${PASSWORD_MAX_LENGTH} 个）`;
}

/** 检查新密码长度；没问题返回 null，有问题返回给用户看的中文提示（与后端的提示文字相同）。 */
export function validateNewPassword(password: string): string | null {
  if (password === "") {
    return "请设置密码";
  }
  const length = passwordLength(password);
  if (length < PASSWORD_MIN_LENGTH) {
    return `密码至少 ${PASSWORD_MIN_LENGTH} 个字符`;
  }
  if (length > PASSWORD_MAX_LENGTH) {
    return `密码最多 ${PASSWORD_MAX_LENGTH} 个字符`;
  }
  return null;
}

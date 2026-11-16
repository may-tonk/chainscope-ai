/**
 * 6 位数字验证码的输入处理和检查（与后端 security/tokens.py 的 CODE_DIGITS = 6 一致）。
 *
 * 注册和忘记密码都用同一种验证码，所以放在 shared 里共用。
 */

export const CODE_LENGTH = 6;

/**
 * 整理用户输入的验证码：只保留数字，最多 6 位。
 * 用户常从邮件里复制出“123 456”或带换行的内容，这里自动去掉空格等非数字字符，
 * 全角数字（１２３）也先转成半角，免得用户自己找哪里输错了。
 */
export function cleanCode(raw: string): string {
  // NFKC 规范化会把全角数字转成普通数字。
  return raw.normalize("NFKC").replace(/\D/g, "").slice(0, CODE_LENGTH);
}

/** 检查验证码是否填完整；没问题返回 null，否则返回中文提示。 */
export function validateCode(code: string): string | null {
  if (code === "") {
    return "请输入验证码";
  }
  if (code.length !== CODE_LENGTH) {
    return `验证码是 ${CODE_LENGTH} 位数字`;
  }
  return null;
}

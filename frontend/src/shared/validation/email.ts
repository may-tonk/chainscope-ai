/**
 * 邮箱格式检查，与后端 python/webapp/core/emails.py 的规则保持一致（方案 4.1 节）。
 *
 * 前端检查只是为了让用户尽早看到“格式不对”，少一次无用的请求；
 * 最终以后端为准：即使绕过这里，后端也会返回 invalid_email。
 *
 * 规则（与后端逐条对应）：
 * - 去掉首尾空格后检查，只接受 ASCII 字符（不接受中文邮箱地址）；
 * - 总长度不超过 254，@ 前面不超过 64，有且只有一个 @；
 * - @ 前面：字母、数字和常见符号，点不能在开头结尾，也不能连续出现；
 * - 域名至少两段（如 example.com），每段由字母数字开头结尾、中间可有连字符，每段最长 63；
 * - 最后一段（顶级域名，如 com、cn）只能是字母，至少 2 个。
 */

export const EMAIL_MAX_LENGTH = 254;
const LOCAL_MAX_LENGTH = 64;
const LOCAL_PART = /^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$/;
const DOMAIN_LABEL = /^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;
const TOP_LEVEL = /^[A-Za-z]{2,}$/;

/** 是否全是 ASCII 字符（编码 0～127）。与后端的 str.isascii() 相同。 */
function isAscii(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    if (value.charCodeAt(i) > 127) {
      return false;
    }
  }
  return true;
}

/**
 * 检查邮箱；没问题返回 null，有问题返回给用户看的中文提示。
 * 参数是用户输入的原文，函数内部会先去掉首尾空格。
 */
export function validateEmail(raw: string): string | null {
  const email = raw.trim();
  if (email === "") {
    return "请输入邮箱";
  }
  if (!isAscii(email) || email.length > EMAIL_MAX_LENGTH) {
    return "邮箱格式不正确";
  }
  const parts = email.split("@");
  if (parts.length !== 2) {
    return "邮箱格式不正确";
  }
  const [local = "", domain = ""] = parts;
  const labels = domain.split(".");
  const topLevel = labels[labels.length - 1] ?? "";
  const valid =
    local.length > 0 &&
    local.length <= LOCAL_MAX_LENGTH &&
    LOCAL_PART.test(local) &&
    labels.length >= 2 &&
    labels.every((label) => DOMAIN_LABEL.test(label)) &&
    TOP_LEVEL.test(topLevel);
  return valid ? null : "邮箱格式不正确";
}

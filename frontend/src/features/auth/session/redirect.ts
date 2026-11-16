/**
 * 登录后跳回“原来想去的页面”时，检查这个目标地址是否安全。
 *
 * 场景：未登录的人打开 /settings，被送到 /login?next=%2Fsettings，登录成功后回到 /settings。
 * 风险：next 来自网址，任何人都能改。如果不检查，攻击者可以发一个
 *   https://我们的网站/login?next=https://钓鱼网站
 * 的链接，用户在真网站登录后被带去钓鱼网站，还以为是同一个网站（“开放重定向”漏洞）。
 *
 * 规则：只接受以单个 “/” 开头的站内路径，其余一律回到工作台首页。
 */

/** 不合格时使用的默认目标：工作台首页。 */
export const DEFAULT_AFTER_LOGIN = "/";

/** 查询参数的名字：/login?next=... */
export const NEXT_PARAM = "next";

/**
 * 检查 next 参数，返回可以安全跳转的站内路径。
 *
 * 拒绝的例子：
 * - null 或空字符串；
 * - "https://evil.example"、"javascript:..."：不以 / 开头；
 * - "//evil.example"：浏览器会把它当成“同协议的另一个网站”；
 * - "/\evil.example"：有些浏览器把反斜杠当成斜杠，等同于上一条；
 * - "/login..."：跳回登录页没有意义，还可能来回跳转。
 */
export function safeNextPath(raw: string | null): string {
  if (!raw || !raw.startsWith("/")) {
    return DEFAULT_AFTER_LOGIN;
  }
  const second = raw.charAt(1);
  if (second === "/" || second === "\\") {
    return DEFAULT_AFTER_LOGIN;
  }
  // 控制字符（换行、制表符等，编码 0～31 和 127）可能被浏览器忽略后拼出意外的地址，一律拒绝。
  if (hasControlCharacter(raw)) {
    return DEFAULT_AFTER_LOGIN;
  }
  if (raw === "/login" || raw.startsWith("/login?") || raw.startsWith("/login/")) {
    return DEFAULT_AFTER_LOGIN;
  }
  return raw;
}

/** 字符串里是否有控制字符（逐个检查字符编码，比正则表达式更直观）。 */
function hasControlCharacter(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code <= 31 || code === 127) {
      return true;
    }
  }
  return false;
}

/** 生成登录页地址，带上登录后要回到的页面。例如 "/settings" → "/login?next=%2Fsettings"。 */
export function loginPathFor(currentPath: string): string {
  if (currentPath === DEFAULT_AFTER_LOGIN) {
    // 本来就要去首页，不需要带参数，地址更干净。
    return "/login";
  }
  return `/login?${NEXT_PARAM}=${encodeURIComponent(currentPath)}`;
}

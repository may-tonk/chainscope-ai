/**
 * 认证相关的接口函数：每个函数对应后端的一个接口（python/webapp/api/routes_auth.py）。
 *
 * 页面和组件只调用这里的函数，不自己拼网址和请求内容；
 * 后端接口有变化时，只需要改这一个文件。
 *
 * 目前有：打开网站、登录、退出，注册三步，忘记密码三步；账户设置在实施第 5 步加入。
 */
import { apiRequest, loadCsrf, setCsrfToken, type CsrfInfo } from "../../shared/api/client";

/** 当前账户的基本信息（对应后端 AccountResponse）。 */
export interface Account {
  user_id: string;
  email: string;
  /** 注册时间，ISO 8601 格式的字符串，例如 "2026-10-25T08:00:00Z"。 */
  created_at: string;
}

/** 登录接口的响应：账户信息，以及登录后新会话对应的 CSRF 令牌。 */
interface LoginResponse {
  account: Account;
  csrf_token: string;
}

/** 后端只返回一句提示的接口（例如退出）。 */
interface MessageResponse {
  message: string;
}

/** 申请验证码的响应：这次验证请求的编号（下一步提交验证码时带上），以及给用户看的提示。 */
export interface ChallengeResponse {
  challenge_id: string;
  message: string;
}

/**
 * 打开网站时调用：取得 CSRF 令牌，并得知当前是否已登录。
 * 没有会话时后端会新建匿名会话（设置 Cookie），所以这一步之后就能提交登录表单。
 */
export function fetchSessionStatus(): Promise<CsrfInfo> {
  return loadCsrf();
}

/** 取当前登录账户的信息。未登录时后端返回 401（not_authenticated）。 */
export function fetchMe(): Promise<Account> {
  return apiRequest<Account>("GET", "/api/auth/me");
}

/**
 * 登录。成功后后端会撤销旧会话、换发新的会话 Cookie，并返回新的 CSRF 令牌，
 * 这里立刻换上新令牌，之后的请求才能通过 CSRF 检查。
 *
 * 失败时抛出 ApiError：账号或密码错误为 authentication_failed（401，不区分是哪一个错），
 * 尝试太多次为 rate_limited（429，附带多少秒后可再试）。
 */
export async function login(email: string, password: string): Promise<Account> {
  const result = await apiRequest<LoginResponse>("POST", "/api/auth/login", {
    email,
    password,
  });
  setCsrfToken(result.csrf_token);
  return result.account;
}

/**
 * 退出当前设备。后端撤销会话并清除 Cookie；即使会话早已失效，也返回成功。
 * 旧的 CSRF 令牌随会话作废，这里清掉，下次修改类请求前会重新获取。
 */
export async function logout(): Promise<void> {
  try {
    await apiRequest<MessageResponse>("POST", "/api/auth/logout");
  } finally {
    setCsrfToken(null);
  }
}

// ── 注册三步（后端 /api/auth/registration/*，方案第 4 节）──

/**
 * 注册第 1 步：提交邮箱，申请验证码。
 *
 * 无论这个邮箱是否已经注册，后端的回复都一样（“如果这个邮箱可以注册，验证码已经发出”），
 * 已注册的邮箱只会收到一封提醒邮件，不会收到验证码。这样别人无法借注册页探测谁注册过。
 *
 * 再次调用就是“重新发送”：后端会作废这个邮箱之前的验证码，返回新的 challenge_id。
 * 失败：邮箱格式不对 invalid_email（400）；发得太频繁 rate_limited（429，同一邮箱 60 秒 1 封）。
 */
export function requestRegistrationCode(email: string): Promise<ChallengeResponse> {
  return apiRequest<ChallengeResponse>("POST", "/api/auth/registration/request", { email });
}

/**
 * 注册第 2 步：提交验证码，换取注册凭证（10 分钟内有效、只能用一次）。
 * 凭证只保存在页面内存里，用于第 3 步；不放进网址或浏览器存储。
 * 失败：验证码错误、过期或已作废统一为 verification_failed（400）。
 */
export async function verifyRegistrationCode(challengeId: string, code: string): Promise<string> {
  const result = await apiRequest<{ registration_token: string }>(
    "POST",
    "/api/auth/registration/verify",
    { challenge_id: challengeId, code },
  );
  return result.registration_token;
}

/**
 * 注册第 3 步：设置密码，创建账户。成功后不会自动登录，需要去登录页登录。
 * 失败：密码不符合规则 password_too_short / password_too_long / password_common（400）；
 * 凭证失效或邮箱已被注册 registration_failed（400，提示重新开始注册）。
 */
export async function completeRegistration(registrationToken: string, password: string): Promise<void> {
  await apiRequest<MessageResponse>("POST", "/api/auth/registration/complete", {
    registration_token: registrationToken,
    password,
  });
}

// ── 忘记密码三步（后端 /api/auth/password-reset/*，方案 2.3 节）──

/**
 * 忘记密码第 1 步：提交邮箱，申请重置验证码。
 *
 * 无论这个邮箱有没有账户，后端的回复都一样（“如果这个邮箱有账户，验证码已经发出”），
 * 邮件在回复之后才在后台发送，别人也没法靠响应快慢判断邮箱是否注册。
 * 没有账户或账户已停用的邮箱不会收到任何邮件。
 *
 * 再次调用就是“重新发送”：后端会作废之前的验证码，返回新的 challenge_id。
 * 失败：邮箱格式不对 invalid_email（400）；发得太频繁 rate_limited（429，同一邮箱 60 秒 1 封）。
 */
export function requestPasswordResetCode(email: string): Promise<ChallengeResponse> {
  return apiRequest<ChallengeResponse>("POST", "/api/auth/password-reset/request", { email });
}

/**
 * 忘记密码第 2 步：提交验证码，换取重置凭证（10 分钟内有效、只能用一次）。
 * 凭证只保存在页面内存里，用于第 3 步；不放进网址或浏览器存储。
 * 失败：验证码错误、过期或已作废统一为 verification_failed（400）。
 */
export async function verifyPasswordResetCode(challengeId: string, code: string): Promise<string> {
  const result = await apiRequest<{ reset_token: string }>(
    "POST",
    "/api/auth/password-reset/verify",
    { challenge_id: challengeId, code },
  );
  return result.reset_token;
}

/**
 * 忘记密码第 3 步：设置新密码。
 *
 * 成功后后端会：只更新密码（账户和账户里的内容都保留），让所有设备退出登录，
 * 并向邮箱发一封“密码已修改”的通知。之后需要用新密码重新登录。
 * 失败：密码不符合规则 password_too_short / password_too_long / password_common（400）；
 * 凭证失效 password_reset_failed（400，提示重新获取验证码）。
 */
export async function completePasswordReset(resetToken: string, newPassword: string): Promise<void> {
  await apiRequest<MessageResponse>("POST", "/api/auth/password-reset/complete", {
    reset_token: resetToken,
    new_password: newPassword,
  });
}

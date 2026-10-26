/**
 * 与后端通信的唯一入口。整个前端只有这个文件直接调用 fetch 向后端发请求。
 *
 * 集中在这里处理的事情：
 * 1. CSRF 令牌：保存在内存里；POST 等修改类请求自动放进 X-CSRF-Token 请求头。
 * 2. 统一错误格式：后端出错时返回 { code, message, request_id }，这里转换成 ApiError，
 *    页面只需要 catch 一种错误类型。
 * 3. 令牌失效自动恢复：遇到 csrf_failed（例如会话过期、后端换了钥），重新取一次令牌后重试一次。
 *
 * 安全约定（方案第 7 节）：
 * - CSRF 令牌只放在这个模块的变量里，不写进 localStorage / sessionStorage / 网址；
 *   刷新页面后变量清空，重新向后端取（后端按会话算出同一个令牌）。
 * - 会话令牌在 HttpOnly Cookie 里，JavaScript 读不到，也不需要读：浏览器会自动带上。
 */

/** 后端统一的错误响应格式（与 python/webapp/api/schemas.py 的 ErrorResponse 对应）。 */
interface ErrorBody {
  code: string;
  message: string;
  request_id: string;
}

/** GET /api/auth/csrf 的响应：CSRF 令牌，以及当前是否已登录。 */
export interface CsrfInfo {
  csrf_token: string;
  authenticated: boolean;
}

/**
 * 所有请求失败都抛出这个错误，页面按 code 判断怎么处理，把 message 显示给用户。
 *
 * - status：HTTP 状态码；连不上服务器时为 0。
 * - code：后端的固定英文编号，例如 authentication_failed、rate_limited、csrf_failed；
 *   前端自己产生的错误用 network_error、service_unavailable。
 * - message：给用户看的中文提示，后端已写好，前端原样显示（不能改写成“邮箱不存在”之类，
 *   否则会泄露邮箱是否注册，方案第 4 节）。
 * - requestId：请求编号，显示给用户，方便对照后端审计和日志排查。
 * - retryAfter：被限速（429）时，多少秒后可以再试。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryAfter: number | null;

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null = null,
    retryAfter: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.retryAfter = retryAfter;
  }
}

/**
 * 把 catch 到的任意错误统一成 ApiError，页面只需处理一种错误类型。
 * 正常情况下请求失败抛出的都是 ApiError；其他错误（代码缺陷等）换成通用提示 fallbackMessage，
 * 不把程序内部的报错原文显示给用户。
 */
export function asApiError(error: unknown, fallbackMessage = "出现意外错误，请重试"): ApiError {
  if (error instanceof ApiError) {
    return error;
  }
  return new ApiError(0, "unexpected_error", fallbackMessage);
}

type Method = "GET" | "POST";

// 会改变服务器状态的请求方法，必须带 CSRF 令牌（后端对这些接口做 CSRF 检查）。
const UNSAFE_METHODS: ReadonlySet<Method> = new Set(["POST"]);

const CSRF_HEADER = "X-CSRF-Token";
const CSRF_PATH = "/api/auth/csrf";

/**
 * “登录已失效”的通知。
 *
 * 任何请求只要收到后端的 not_authenticated（401，会话过期、被撤销、在别处退出），
 * 都会通知这里登记的监听者。全局登录状态（AuthProvider）登记一次，就能统一处理：
 * 切换为未登录、跳到登录页并提示“登录已过期”。
 * 以后新增的功能（研究会话、报告等）调用接口时自动受益，不需要每个页面各写一遍。
 */
type SessionExpiredListener = () => void;
const sessionExpiredListeners = new Set<SessionExpiredListener>();

/**
 * 登记一个监听者，在任何请求发现“登录已失效”时被调用。
 * 返回一个取消登记的函数（组件卸载时调用，避免重复登记）。
 */
export function onSessionExpired(listener: SessionExpiredListener): () => void {
  sessionExpiredListeners.add(listener);
  return () => {
    sessionExpiredListeners.delete(listener);
  };
}

/** 当前的 CSRF 令牌；null 表示还没取或已作废，下次修改类请求前会先去取。 */
let csrfToken: string | null = null;

/** 正在进行中的“取令牌”请求。多个请求同时需要令牌时共用这一次，不重复向后端要。 */
let pendingCsrf: Promise<CsrfInfo> | null = null;

/**
 * 设置或清除内存里的 CSRF 令牌。
 * - 登录成功：后端换了新会话，响应里带新令牌，用它替换旧的；
 * - 退出、改密码、注销：后端撤销了会话，旧令牌作废，设为 null。
 */
export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

/**
 * 向后端取 CSRF 令牌（GET /api/auth/csrf），并记在内存里。
 * 浏览器还没有有效会话时，后端会顺便建一个匿名会话并设置 Cookie。
 * 返回值里的 authenticated 用来在打开网站时判断是否已登录。
 */
export function loadCsrf(): Promise<CsrfInfo> {
  if (pendingCsrf === null) {
    pendingCsrf = send<CsrfInfo>("GET", CSRF_PATH, undefined, false)
      .then((info) => {
        csrfToken = info.csrf_token;
        return info;
      })
      .finally(() => {
        // 不论成功失败都清掉，下次需要时可以重新发起。
        pendingCsrf = null;
      });
  }
  return pendingCsrf;
}

/**
 * 发送请求并返回解析好的 JSON 数据。出错时抛出 ApiError。
 *
 * @param method 请求方法，目前后端只用到 GET 和 POST。
 * @param path 以 /api/ 开头的站内路径；不接受完整网址，Cookie 不会被发到别的网站。
 * @param body 请求内容（会转成 JSON）；GET 请求不传。
 */
export function apiRequest<T>(method: Method, path: string, body?: unknown): Promise<T> {
  return send<T>(method, path, body, true);
}

/**
 * apiRequest 的实际实现。allowRetry 控制遇到 csrf_failed 时是否允许“重新取令牌再试一次”，
 * 重试的那一次传 false，保证最多只重试一次，不会无限循环。
 */
async function send<T>(
  method: Method,
  path: string,
  body: unknown,
  allowRetry: boolean,
): Promise<T> {
  if (!path.startsWith("/api/")) {
    // 编程错误，不是用户能遇到的情况：防止把带 Cookie 的请求发到站外。
    throw new Error(`apiRequest 只接受 /api/ 开头的站内路径：${path}`);
  }

  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (UNSAFE_METHODS.has(method)) {
    // 修改类请求必须带令牌；还没有就先取（例如刚打开页面就点了提交）。
    const token = csrfToken ?? (await loadCsrf()).csrf_token;
    headers[CSRF_HEADER] = token;
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      // 只在同源请求中带 Cookie（我们的 /api 与页面同源），不会带到别的网站。
      credentials: "same-origin",
      // 接口数据不使用浏览器缓存，每次都向后端要最新结果（例如登录状态）。
      cache: "no-store",
    });
  } catch {
    // fetch 本身抛错说明根本没连上：断网、后端没启动、被浏览器拦截等。
    throw new ApiError(0, "network_error", "无法连接服务器，请检查网络后重试");
  }

  if (response.ok) {
    return (await readJson(response)) as T;
  }

  const error = await toApiError(response);
  if (error.code === "csrf_failed" && allowRetry && UNSAFE_METHODS.has(method)) {
    // 令牌失效（会话过期、刚退出、后端换钥）：重新取一次令牌后重试。
    // 后端在业务逻辑之前就拒绝了这类请求，什么都没执行，所以重试是安全的，不会重复登录或重复发信。
    setCsrfToken(null);
    await loadCsrf();
    return send<T>(method, path, body, false);
  }
  if (error.code === "not_authenticated") {
    // 登录已失效：通知全局登录状态。错误照常抛出，调用方仍可按需处理（例如显示提示）。
    sessionExpiredListeners.forEach((listener) => listener());
  }
  throw error;
}

/** 读取响应里的 JSON；响应没有内容或不是 JSON 时返回 undefined。 */
async function readJson(response: Response): Promise<unknown> {
  const type = response.headers.get("Content-Type") ?? "";
  if (!type.includes("application/json")) {
    return undefined;
  }
  return response.json();
}

/**
 * 把失败的响应转换成 ApiError。
 * 后端的错误都是统一格式；但如果后端没启动，开发服务器的转发会返回非 JSON 的错误页，
 * 这时给出通用的“服务暂时不可用”，不把看不懂的内容显示给用户。
 */
async function toApiError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("X-Request-ID");
  const retryAfter = parseRetryAfter(response.headers.get("Retry-After"));
  let body: unknown;
  try {
    body = await readJson(response);
  } catch {
    body = undefined;
  }
  if (isErrorBody(body)) {
    return new ApiError(response.status, body.code, body.message, body.request_id, retryAfter);
  }
  return new ApiError(
    response.status,
    "service_unavailable",
    "服务暂时不可用，请稍后再试",
    requestId,
    retryAfter,
  );
}

/** 检查一个值是不是后端的统一错误格式（三个字段都是字符串）。 */
function isErrorBody(value: unknown): value is ErrorBody {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.code === "string" &&
    typeof candidate.message === "string" &&
    typeof candidate.request_id === "string"
  );
}

/** 解析 Retry-After 响应头（秒数）。没有或格式不对时返回 null。 */
function parseRetryAfter(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value.trim())) {
    return null;
  }
  return Number(value.trim());
}

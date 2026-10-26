/**
 * 全局登录状态的“形状”定义，以及读取它的 useAuth()。
 *
 * React 的 Context 可以理解为“全局共享的一块数据”：最外层的 AuthProvider 负责维护它，
 * 任何页面、组件调用 useAuth() 就能读到当前登录状态，并调用登录、退出。
 * 这样不需要把登录状态一层层手动传给每个组件。
 */
import { createContext, useContext } from "react";

import type { ApiError } from "../../../shared/api/client";
import type { Account } from "../api";

/**
 * 从“已登录”变为“未登录”的原因。取值与 loginNotices.ts 里的提示编号同名：
 * 提示表里有同名提示的，页面保护跳到登录页时会显示它；没有的（目前是 signed_out）只做记录，不显示提示。
 * - signed_out：自己点“退出登录”；
 * - expired：登录已失效（会话过期、被撤销、在别处退出所有设备）；
 * - password_changed / signed_out_everywhere / account_deleted：在设置页完成对应操作后。
 */
export type SignedOutReason =
  | "signed_out"
  | "expired"
  | "password_changed"
  | "signed_out_everywhere"
  | "account_deleted";

/**
 * 登录状态的四种可能，任何时刻只处于其中一种：
 * - loading：刚打开网站，正在向后端确认是否已登录；
 * - anonymous：未登录；reason 说明“为什么变成未登录”，用来在登录页显示对应提示：
 *   signed_out 是用户自己退出，expired 是登录已失效；刚打开网站时就未登录则没有 reason；
 * - authenticated：已登录，带有账户信息；
 * - error：无法确认（后端没启动、数据库不可用等），显示错误和“重试”按钮。
 *
 * 用这种写法，TypeScript 会强制在用 account 之前先判断 status === "authenticated"，
 * 不会出现“以为已登录、其实 account 是空的”这种错误。
 */
export type AuthState =
  | { status: "loading" }
  | { status: "anonymous"; reason?: SignedOutReason }
  | { status: "authenticated"; account: Account }
  | { status: "error"; error: ApiError };

/** 通过 useAuth() 拿到的内容：当前状态，以及会改变状态的几个操作。 */
export interface AuthContextValue {
  state: AuthState;
  /** 登录。成功后状态变为 authenticated；失败时抛出 ApiError，由登录页显示提示。 */
  login(email: string, password: string): Promise<void>;
  /** 退出当前设备。成功后状态变为 anonymous；失败时抛出 ApiError，状态不变。 */
  logout(): Promise<void>;
  /** 重新向后端确认登录状态（错误页面的“重试”按钮使用）。 */
  reload(): Promise<void>;
  /**
   * 后端已经结束了这个会话之后（例如修改密码、退出所有设备、注销账户成功），
   * 把页面切换为未登录，并记下原因；页面保护随后跳到登录页，显示对应提示。
   */
  endSession(reason: SignedOutReason): void;
}

/** 默认值为 null：如果某个组件不在 AuthProvider 里面，useAuth() 会明确报错，而不是悄悄拿到空数据。 */
export const AuthContext = createContext<AuthContextValue | null>(null);

/** 在任意组件里读取登录状态和登录、退出操作。 */
export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth() 必须在 <AuthProvider> 内部使用");
  }
  return value;
}

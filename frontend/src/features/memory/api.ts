/** 持久聊天 API：唯一网络入口仍是 shared/api/client，历史只从服务端读取。 */
import { ApiError, apiRequest, apiStreamRequest } from "../../shared/api/client";
import { consumeChatResponse } from "../research/api/chat";
import type { ChatEvent } from "../research/contracts/session";

export interface Conversation {
  conversation_id: string;
  title: string;
  revision: number;
  updated_at: string;
}
export interface StoredMessage {
  message_id: string;
  conversation_id: string;
  turn_id: string;
  sequence: number;
  role: "user" | "assistant";
  status: "queued" | "running" | "completed" | "stopped" | "failed";
  content: string;
}
export interface Snapshot {
  conversation: Conversation;
  messages: StoredMessage[];
  next_sequence: number;
  has_more: boolean;
  poll_after_ms: number;
}

/** 服务端响应也校验核心字段，失败时不把内部响应正文展示给用户。 */
function invalid(): never { throw new ApiError(0, "memory_protocol_error", "对话数据格式异常，请刷新重试。"); }
/** 将未知响应收窄为对象，避免页面直接解引用不可信字段。 */
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object") return invalid();
  return value as Record<string, unknown>;
}
/** 只投影页面使用的会话字段，修订号必须是正整数。 */
function conversation(value: unknown): Conversation {
  const row = object(value);
  if (typeof row.conversation_id !== "string" || typeof row.title !== "string" || !Number.isSafeInteger(row.revision) || (row.revision as number) < 1 || typeof row.updated_at !== "string") return invalid();
  return row as unknown as Conversation;
}

/** 创建时只发送标题；用户归属来自登录 Cookie。 */
export async function createConversation(question = "新对话"): Promise<Conversation> {
  // Unicode 字符逐个截取，160 是接口字段上限，实际标题长度由后端 YAML 决定。
  const title = Array.from((question.split("\n")[0] ?? "").trim()).slice(0, 160).join("") || "新对话";
  const body = object(await apiRequest("POST", "/api/memory/conversations", { title }));
  return conversation(body.conversation);
}

/** 会话列表使用后端默认分页数量，界面不再复制 YAML 中的水位。 */
export async function listConversations(before?: Conversation): Promise<Conversation[]> {
  const query = before ? `?before_time=${encodeURIComponent(before.updated_at)}&before_id=${encodeURIComponent(before.conversation_id)}` : "";
  const body = object(await apiRequest("GET", `/api/memory/conversations${query}`));
  if (!Array.isArray(body.conversations)) return invalid();
  return body.conversations.map(conversation);
}

/** 按稳定消息序号读取一页，检查归属标识和递增顺序。 */
export async function getSnapshot(id: string, after = 0): Promise<Snapshot> {
  const body = object(await apiRequest("GET", `/api/memory/conversations/${encodeURIComponent(id)}?after_sequence=${after}`));
  const owner = conversation(body.conversation);
  if (owner.conversation_id !== id || !Array.isArray(body.messages) || typeof body.has_more !== "boolean" || !Number.isSafeInteger(body.next_sequence)) return invalid();
  let sequence = after;
  const messages = body.messages.map((value) => {
    const row = object(value);
    if (typeof row.message_id !== "string" || typeof row.turn_id !== "string" || row.conversation_id !== id || !Number.isSafeInteger(row.sequence) || (row.sequence as number) <= sequence || !["user", "assistant"].includes(String(row.role)) || !["queued", "running", "completed", "stopped", "failed"].includes(String(row.status)) || typeof row.content !== "string") return invalid();
    sequence = row.sequence as number;
    return row as unknown as StoredMessage;
  });
  if (body.next_sequence !== sequence || (body.has_more && messages.length === 0) || !Number.isSafeInteger(body.poll_after_ms) || (body.poll_after_ms as number) < 1) return invalid();
  return { conversation: owner, messages, next_sequence: sequence, has_more: body.has_more, poll_after_ms: body.poll_after_ms as number };
}

/** 一次发送只携带新消息、幂等键和预期修订，禁止提交历史或身份。 */
export async function streamTurn(id: string, revision: number, requestId: string, content: string, signal: AbortSignal, receive: (event: ChatEvent) => void): Promise<void> {
  const response = await apiStreamRequest(`/api/memory/conversations/${encodeURIComponent(id)}/turns`, {
    conversation_id: id, expected_revision: revision, client_request_id: requestId, content,
  }, signal);
  return consumeChatResponse(response, signal, (event) => {
    if (!Number.isSafeInteger(event.request_revision) || (event.request_revision ?? 0) < 1) return invalid();
    receive(event);
  });
}

/** 显式停止帮助快速收尾；浏览器离页断开还由后端取消保护兜底。 */
export async function stopTurn(id: string, turnId: string, revision: number): Promise<void> {
  await apiRequest("POST", `/api/memory/conversations/${encodeURIComponent(id)}/turns/${encodeURIComponent(turnId)}/stop`, { expected_revision: revision });
}

/** 删除当前用户会话，版本冲突由后端明确拒绝。 */
export async function deleteConversation(row: Conversation): Promise<void> {
  await apiRequest("POST", `/api/memory/conversations/${encodeURIComponent(row.conversation_id)}/delete`, { expected_revision: row.revision });
}

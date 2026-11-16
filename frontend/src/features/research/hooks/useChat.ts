/** 当前页面聊天：分轮隔离、停止、重试和明确提供的多轮上下文，不落盘保存历史。 */
import { useEffect, useRef, useState } from "react";
import { asApiError } from "../../../shared/api/client";
import { streamChat } from "../api/chat";
import type { ChatEvent, ChatInput, ChatTurn } from "../contracts/session";

/** transport 仅用于测试注入，正常调用始终使用真实站内接口。 */
export function useChat(transport = streamChat) {
  const [turns, setTurns] = useState<readonly ChatTurn[]>([]);
  const data = useRef<readonly ChatTurn[]>([]);
  const active = useRef<{ id: string; controller: AbortController } | null>(
    null
  );
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      active.current?.controller.abort();
      active.current = null;
    };
  }, []);

  /** ref 保存事件处理的最新快照，React state 负责绘制。 */
  function update(id: string, change: Partial<ChatTurn>) {
    data.current = data.current.map((turn) =>
      turn.id === id ? { ...turn, ...change } : turn
    );
    if (mounted.current) setTurns(data.current);
  }
  /** 先作废回调再 abort，旧请求晚到内容不能覆盖下一轮。 */
  function stop() {
    const previous = active.current;
    active.current = null;
    if (previous) {
      previous.controller.abort();
      const turn = data.current.find((item) => item.id === previous.id);
      // 终态可能先于 HTTP 连接关闭；不能把已完成的回答改成停止并丢出后续上下文。
      if (turn?.status === "waiting" || turn?.status === "streaming") {
        update(previous.id, { status: "stopped" });
      }
    }
  }
  /** 每轮回调核对自己的 controller，卸载或账户切换后不能继续发布。 */
  async function run(id: string, input: ChatInput) {
    const controller = new AbortController();
    active.current = { id, controller };
    try {
      await transport(input, controller.signal, (event: ChatEvent) => {
        if (!mounted.current || active.current?.controller !== controller)
          return;
        const payload = event.payload;
        const current = data.current.find((turn) => turn.id === id);
        if (!current) return;
        switch (payload.type) {
          case "message.delta":
            update(id, {
              answer: current.answer + payload.text,
              status: "streaming",
            });
            break;
          case "turn.completed":
            update(id, { answer: payload.text, status: "complete" });
            break;
          case "turn.failed":
            update(id, { status: "failed", error: payload.message });
            break;
          case "turn.interrupted":
            update(id, { status: "stopped" });
            break;
          case "decision.ready":
            update(id, {
              dispatchPending: payload.dispatch_status === "not_connected",
            });
            break;
        }
      });
    } catch (error) {
      if (
        mounted.current &&
        active.current?.controller === controller &&
        !controller.signal.aborted
      )
        update(id, {
          status: "failed",
          error: asApiError(error, "回复连接中断，本轮未能完成，请重试。")
            .message,
        });
    } finally {
      if (active.current?.controller === controller) active.current = null;
    }
  }
  /** 已完成回答进入后续上下文；失败/中止的部分文本只用于当前页展示。 */
  function send(question: string) {
    if (!question.trim()) return;
    stop();
    const messages: ChatInput["messages"][number][] = [];
    for (const turn of data.current) {
      messages.push({ role: "user", content: turn.question });
      if (turn.status === "complete")
        messages.push({ role: "assistant", content: turn.answer });
    }
    messages.push({ role: "user", content: question });
    const id = crypto.randomUUID();
    const request = { messages };
    data.current = [
      ...data.current,
      {
        id,
        question,
        answer: "",
        status: "waiting",
        dispatchPending: false,
        error: null,
        request,
      },
    ];
    setTurns(data.current);
    void run(id, request);
  }
  /** 手动重试复用原问题快照，不重复插入用户消息。 */
  function retry() {
    const last = data.current.at(-1);
    if (!last || last.status !== "failed") return;
    // 失败终态已收到时允许立即重试，同时关闭尚未结束的旧 HTTP 读取。
    stop();
    update(last.id, {
      answer: "",
      status: "waiting",
      error: null,
      dispatchPending: false,
    });
    void run(last.id, last.request);
  }
  return {
    turns,
    send,
    stop,
    retry,
    streaming: turns.some(
      (turn) => turn.status === "waiting" || turn.status === "streaming"
    ),
  };
}

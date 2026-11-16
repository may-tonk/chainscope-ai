/** 会话界面与事件协议回归：参数、停止语义、失败保留和旧事件隔离。 */
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { ResearchSessionService, SessionActions, SessionEvent, SessionLoadState } from "../src/features/research/contracts/session";
import { ResearchSessionPage } from "../src/features/research/pages/ResearchSessionPage";
import { sessionReducer } from "../src/features/research/state/sessionState";
import { useResearchSession } from "../src/features/research/hooks/useResearchSession";
import { snapshot } from "./researchSession.fixtures";

/** 提供显式回调供交互测试观察，未挂载到生产路由。 */
function actions(): SessionActions { return { send: vi.fn().mockResolvedValue(undefined), clarify: vi.fn().mockResolvedValue(undefined), cancel: vi.fn().mockResolvedValue(undefined) }; }

describe("研究会话交互", () => {
  it("未接入时保留草稿，不制造用户消息或后台结果", () => {
    render(<MemoryRouter><ResearchSessionPage state={{ status: "unavailable", snapshot: null }} actions={null} initialDraft={{ question: "调查这个哈希", objects: [], time: null }} /></MemoryRouter>);
    expect(screen.getByRole("textbox", { name: "继续你的研究" })).toHaveValue("调查这个哈希");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(screen.queryByRole("list", { name: "研究消息" })).not.toBeInTheDocument();
  });

  it("本地相对时间选择不编造截止，并可从草稿移除", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><ResearchSessionPage state={{ status: "unavailable", snapshot: null }} actions={null} /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "补充时间（按需）" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("接入研究服务后显示实际数据截止时间。")).toBeInTheDocument();
    await user.type(screen.getByRole("spinbutton", { name: "时长" }), "24");
    await user.click(screen.getByRole("button", { name: "应用到草稿" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "最近 24 小时" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "移除时间" }));
    expect(screen.getByRole("button", { name: "补充时间（按需）" })).toBeInTheDocument();
  });

  it("Agent 时间追问返回原编号和修订，不按浏览器当前时刻改写", async () => {
    const user = userEvent.setup(), api = actions();
    const request = { id: "clarification-1", requestRevision: 1, question: "你想研究多少小时或天？", dataCutoffTime: "2026-11-07T06:00:00Z" };
    render(<MemoryRouter><ResearchSessionPage state={{ status: "ready", snapshot: snapshot({ status: "needs_input", clarification: request }) }} actions={api} /></MemoryRouter>);
    await user.type(screen.getByRole("spinbutton", { name: "时长" }), "3");
    await user.selectOptions(screen.getByRole("combobox", { name: "单位" }), "day");
    await user.click(screen.getByRole("button", { name: "提交时间" }));
    expect(api.clarify).toHaveBeenCalledWith(request, { mode: "relative", amount: 3, unit: "day" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("已确认的停止请求不冒充已取消，后端运行状态保持", async () => {
    const user = userEvent.setup(), api = actions();
    render(<MemoryRouter><ResearchSessionPage state={{ status: "ready", snapshot: snapshot() }} actions={api} /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "停止研究" }));
    expect(api.cancel).toHaveBeenCalledOnce();
    expect(await screen.findByRole("button", { name: "已请求停止" })).toBeDisabled();
    expect(screen.getByText("研究中")).toBeInTheDocument();
    expect(screen.queryByText("已取消")).not.toBeInTheDocument();
  });

  it("后台运行仍可发送追问；失败保留内容且不泄露原始异常", async () => {
    const user = userEvent.setup(), api = actions();
    vi.mocked(api.send).mockRejectedValue(new Error("private-connection-detail"));
    render(<MemoryRouter><ResearchSessionPage state={{ status: "ready", snapshot: snapshot() }} actions={api} /></MemoryRouter>);
    await user.type(screen.getByRole("textbox"), "再看一下发行活动");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("草稿已保留");
    expect(screen.getByRole("textbox")).toHaveValue("再看一下发行活动");
    expect(screen.queryByText("private-connection-detail")).not.toBeInTheDocument();
  });

  it("服务断开后不继续发送，展示最后状态且不声称任务终止", () => {
    render(<MemoryRouter><ResearchSessionPage state={{ status: "ready", snapshot: snapshot() }} actions={actions()} disconnected /></MemoryRouter>);
    expect(screen.getByRole("alert")).toHaveTextContent("断开连接不代表后台任务已停止");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });
});

describe("事件隔离与恢复", () => {
  const initial: SessionLoadState = { status: "ready", snapshot: snapshot() };
  const event: SessionEvent = { eventId: "event-11", sessionId: "session-a", requestRevision: 1, sequence: 11, type: "message", message: { id: "message-a", role: "assistant", text: "测试文本", evidenceIds: [] } };
  it.each([
    { ...event, sessionId: "someone-else" },
    { ...event, requestRevision: 0 },
    { ...event, sequence: 9 },
  ])("忽略跨会话、旧修订和过期事件", (value) => { expect(sessionReducer(initial, { type: "event", event: value })).toBe(initial); });
  it("重复投递不追加消息，同编号的新内容执行更新", () => {
    const first = sessionReducer(initial, { type: "event", event });
    expect(sessionReducer(first, { type: "event", event })).toBe(first);
    const next = sessionReducer(first, { type: "event", event: { ...event, sequence: 12, message: { ...event.message, text: "更新文本" } } });
    expect(next.snapshot?.messages).toHaveLength(1);
    expect(next.snapshot?.messages[0]?.text).toBe("更新文本");
  });
  it("新修订由完整快照建立，旧加载结果不能覆盖", () => {
    const next = sessionReducer(initial, { type: "snapshot", snapshot: snapshot({ requestRevision: 2, sequence: 20 }) });
    expect(sessionReducer(next, { type: "snapshot", snapshot: snapshot() })).toBe(next);
  });
  it("异步读取完成后订阅，卸载中止连接并忽略晚到事件", async () => {
    let receive!: (value: SessionEvent) => void;
    let signal!: AbortSignal;
    const unsubscribe = vi.fn();
    const service: ResearchSessionService = {
      load: vi.fn(async (_id, inputSignal) => { signal = inputSignal; return snapshot(); }),
      subscribe: vi.fn((_id, _sequence, inputReceive) => { receive = inputReceive; return unsubscribe; }),
      send: vi.fn().mockResolvedValue(snapshot()), clarify: vi.fn(), cancel: vi.fn(),
    };
    const { result, unmount } = renderHook(() => useResearchSession("session-a", service));
    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    expect(service.subscribe).toHaveBeenCalledWith("session-a", 10, expect.any(Function), expect.any(Function));
    unmount();
    expect(signal.aborted).toBe(true);
    expect(unsubscribe).toHaveBeenCalledOnce();
    act(() => receive(event));
    expect(result.current.state.snapshot?.messages).toHaveLength(0);
  });
  it("订阅建立失败保留已读快照，并明确标记断线", async () => {
    const service: ResearchSessionService = {
      load: vi.fn().mockResolvedValue(snapshot()),
      subscribe: vi.fn(() => { throw new Error("connection failed"); }),
      send: vi.fn(), clarify: vi.fn(), cancel: vi.fn(),
    };
    const { result } = renderHook(() => useResearchSession("session-a", service));
    await waitFor(() => expect(result.current.disconnected).toBe(true));
    expect(result.current.state.status).toBe("ready");
    expect(result.current.state.snapshot?.id).toBe("session-a");
  });
});

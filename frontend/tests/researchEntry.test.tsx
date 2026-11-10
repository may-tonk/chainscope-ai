/** 研究入口交互测试：防止假提交、草稿丢失和未接入状态被当作真实空数据。 */
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { HomePage } from "../src/features/workspace/pages/HomePage";
import { RecentResearch } from "../src/features/workspace/components/RecentResearch";
import type { ResearchDraft } from "../src/features/workspace/types/research";

describe("研究入口", () => {
  it("未接入服务时允许编辑，但不提交或显示伪造历史", async () => {
    const user = userEvent.setup();
    render(<HomePage upcoming={[]} />);
    await user.type(screen.getByRole("textbox", { name: "你想研究什么？" }), "分析 BEM");
    expect(screen.getByRole("button", { name: "开始研究" })).toBeDisabled();
    expect(screen.getByText("研究记录尚未接入")).toBeInTheDocument();
    expect(screen.queryByText("还没有研究记录")).not.toBeInTheDocument();
  });

  it("示例填入可编辑问题；移除对象不删除原问题；清空只影响当前草稿", async () => {
    const user = userEvent.setup();
    render(<HomePage upcoming={[]} />);
    await user.click(screen.getByRole("button", { name: /BEM 的活动发生了什么变化/ }));
    const input = screen.getByRole("textbox", { name: "你想研究什么？" });
    expect(input).toHaveFocus();
    expect(input).toHaveValue("帮我分析 BEM 最近的链上活动变化，区分普通转账与零地址发出事件。");
    await user.click(screen.getByRole("button", { name: "移除 BEM 研究对象" }));
    expect(screen.queryByRole("list", { name: "已引用的研究对象" })).not.toBeInTheDocument();
    expect(input).toHaveValue("帮我分析 BEM 最近的链上活动变化，区分普通转账与零地址发出事件。");
    await user.click(screen.getByRole("button", { name: "清空" }));
    expect(input).toHaveValue("");
  });

  it("交易示例不生成假哈希，切换示例移除不再适用的对象", async () => {
    const user = userEvent.setup();
    render(<HomePage upcoming={[]} />);
    await user.click(screen.getByRole("button", { name: /BEM 的活动发生了什么变化/ }));
    await user.click(screen.getByRole("button", { name: /这笔交易里发生了什么/ }));
    expect(screen.getByRole("textbox")).toHaveValue("请帮我分析这笔 BSC 交易：");
    expect(screen.queryByRole("button", { name: "移除 BEM 研究对象" })).not.toBeInTheDocument();
  });

  it("真实提交适配器接收完整上下文，提交中禁止重复点击", async () => {
    const user = userEvent.setup();
    let finish!: () => void;
    const submit = vi.fn<(draft: ResearchDraft) => Promise<void>>(() => new Promise<void>((resolve) => { finish = resolve; }));
    render(<HomePage upcoming={[]} execution={{ status: "available", submit }} />);
    await user.click(screen.getByRole("button", { name: /BEM 的活动发生了什么变化/ }));
    await user.click(screen.getByRole("button", { name: "开始研究" }));
    expect(submit).toHaveBeenCalledTimes(1);
    expect(submit.mock.calls[0]?.[0].contexts[0]).toMatchObject({ chainId: 56, symbol: "BEM" });
    expect(screen.getByRole("button", { name: "开始研究" })).toBeDisabled();
    await act(async () => { finish(); });
    expect(screen.getByRole("button", { name: "开始研究" })).toBeEnabled();
  });

  it("失败保留问题，不把原始异常泄露到页面", async () => {
    const user = userEvent.setup();
    const submit = vi.fn().mockRejectedValue(new Error("private-provider-detail"));
    render(<HomePage upcoming={[]} execution={{ status: "available", submit }} />);
    await user.type(screen.getByRole("textbox"), "检查这个地址");
    await user.click(screen.getByRole("button", { name: "开始研究" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("问题已保留");
    expect(screen.getByRole("textbox")).toHaveValue("检查这个地址");
    expect(screen.queryByText("private-provider-detail")).not.toBeInTheDocument();
  });

  it("页面重新挂载后不继承上一次组件树的私人草稿", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<HomePage upcoming={[]} />);
    await user.type(screen.getByRole("textbox"), "私人研究问题");
    unmount();
    render(<HomePage upcoming={[]} />);
    expect(screen.getByRole("textbox")).toHaveValue("");
  });
});

describe("研究记录状态", () => {
  it("真实空记录和读取失败分别表达", () => {
    const { rerender } = render(<RecentResearch state={{ status: "ready", items: [] }} />);
    expect(screen.getByText("还没有研究记录")).toBeInTheDocument();
    rerender(<RecentResearch state={{ status: "error", message: "请稍后重试。" }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("暂时无法读取研究记录");
    expect(screen.queryByText("还没有研究记录")).not.toBeInTheDocument();
  });
});
